"""售后工单评测服务（需求书 §9）。

需求书要求"建立标注工单集，覆盖各问题类型和风险等级"，指标包括：
问题分类 Macro-F1、政策 Recall@5、证据支持率、回复采纳率、人工转派率、端到端 P95、
重复写操作率、每单成本；验收还必须覆盖正常工单、缺订单号、订单不存在、政策冲突、
物流接口超时、重复审批请求、低置信度、L3/L4 风险与租户越权。

## 怎么做（三条设计取舍）

1. **离线可跑、零成本**：全部用 stub 模型 + 临时库（`scripts/run_ticket_evaluation.py`），
   与诊断评测集同一套路。真实模型评测要显式开关，避免"以为在跑离线基线，实际在烧钱"。
2. **指标只由代码算**：分类 F1、审批门禁召回、政策版本命中这些都用 `checks` 里的事实聚合，
   不接受"看起来不错"的主观判断。**基线数字不得写成生产效果**（需求书 §11 的纪律）。
3. **安全指标优先**：`approval_gate_recall`（该审批的是否都进了审批）与
   `expired_policy_leak_rate`（是否引用了失效/未生效政策）必须为 1.0 / 0，
   它们比通过率更重要 —— 前者错会放出未经审批的赔付，后者错会写出合规事故。
"""

from __future__ import annotations

import json
import hashlib
import time
from collections import Counter, defaultdict
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import SessionLocal
from app.models.evaluation import EvaluationRun
from app.models.identity import User
from app.services.ticket_analysis import FactIncomplete, start_analysis
from app.services.ticket_eval_dataset import TicketDataset
from app.services.ticket_service import TicketService

CASES_PATH = Path(__file__).resolve().parents[2] / "tests" / "evaluation" / "tickets.json"
DATASET_VERSION = "tickets-1.1.0"

# 被视为"已失效/未生效"的政策版本（引用到它们就是合规事故）
FORBIDDEN_POLICY_VERSIONS = {"v1.0", "v4.0"}


class TicketEvaluationService:
    def __init__(self, session: Session, *, settings=None, session_factory=None) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.session_factory = session_factory or SessionLocal

    # ------------------------------------------------------------------ 数据集
    @staticmethod
    def _stratified_sample(cases: list[dict], sample: int) -> list[dict]:
        """按 group 轮转抽样，保证小样本评测覆盖每个验收维度。

        直接 `cases[:50]` 会取到前 50 条 `normal`（数据集按组连续排列），
        真实模型评测就会被单一场景主导。这里按组轮转、组内保持原顺序，
        结果确定可复现，不需要随机种子。
        """
        if sample <= 0 or sample >= len(cases):
            return list(cases)
        buckets: dict[str, list[dict]] = {}
        order: list[str] = []
        for case in cases:
            group = str(case.get("group") or "other")
            if group not in buckets:
                buckets[group] = []
                order.append(group)
            buckets[group].append(case)
        picked: list[dict] = []
        depth = 0
        while len(picked) < sample:
            progressed = False
            for group in order:
                if depth < len(buckets[group]):
                    picked.append(buckets[group][depth])
                    progressed = True
                    if len(picked) >= sample:
                        break
            if not progressed:
                break
            depth += 1
        return picked

    @staticmethod
    def load_cases(
        *,
        limit: int | None = None,
        groups: list[str] | None = None,
        sample: int | None = None,
    ) -> list[dict]:
        payload = json.loads(CASES_PATH.read_text(encoding="utf-8"))
        cases = payload.get("cases", [])
        if groups:
            cases = [case for case in cases if case.get("group") in groups]
        if sample:
            return TicketEvaluationService._stratified_sample(cases, sample)
        return cases[: limit or len(cases)]

    # ------------------------------------------------------------------ 运行
    def run(
        self,
        *,
        tenant_id: str,
        name: str | None = None,
        limit: int | None = None,
        groups: list[str] | None = None,
        sample: int | None = None,
        dataset: TicketDataset | None = None,
    ) -> EvaluationRun:
        if dataset is None:
            cases = self.load_cases(limit=limit, groups=groups, sample=sample)
            dataset_metadata = {
                "dataset_version": DATASET_VERSION,
                "dataset_kind": "internal_generated_regression",
                "dataset_sha256": hashlib.sha256(CASES_PATH.read_bytes()).hexdigest(),
                "case_count": len(self.load_cases()),
            }
        else:
            cases = list(dataset.cases)
            if groups:
                cases = [case for case in cases if case.get("group") in groups]
            if sample:
                cases = self._stratified_sample(cases, sample)
            else:
                cases = cases[: limit or len(cases)]
            dataset_metadata = dataset.safe_metadata()
        run_row = EvaluationRun(
            tenant_id=tenant_id,
            name=name or f"售后工单评测 {len(cases)} 条",
            dataset_version=dataset_metadata["dataset_version"],
            workflow_version=self.settings.workflow_version,
            prompt_version=self.settings.prompt_version,
            embedding_version=self.settings.embedding_version,
            case_count=len(cases),
            status="running",
        )
        self.session.add(run_row)
        self.session.commit()

        details: list[dict] = []
        latencies: list[int] = []
        tokens: list[int] = []
        try:
            for case in cases:
                detail, latency, used_tokens = self._run_case(
                    case,
                    tenant_id=tenant_id,
                    evaluation_key=_evaluation_case_key(run_row.id, case["case_id"]),
                )
                details.append(detail)
                latencies.append(latency)
                tokens.append(used_tokens)
            metrics = self._aggregate(
                details, latencies, tokens, dataset_metadata=dataset_metadata
            )
            run_row.metrics = metrics
            run_row.details = details
            run_row.avg_latency_ms = round(sum(latencies) / len(latencies), 1) if latencies else None
            run_row.status = "completed"
        except Exception as exc:  # 评测失败也要留痕
            run_row.status = "failed"
            run_row.metrics = {"error": f"{type(exc).__name__}: {exc}"}
            run_row.details = details
        finally:
            self.session.commit()
        return run_row

    # ------------------------------------------------------------------ 单条用例
    def _run_case(
        self, case: dict, *, tenant_id: str, evaluation_key: str | None = None
    ) -> tuple[dict, int, int]:
        started = time.perf_counter()
        expect = case.get("expect") or {}
        checks: dict[str, bool] = {}
        observed: dict = {}
        error: str | None = None

        user = (
            self.session.query(User).filter(User.username == case.get("user", "ops")).first()
        )
        if user is None:
            raise RuntimeError(f"评测依赖的账号不存在: {case.get('user')}（请先执行 python -m app.db.seed）")

        try:
            if case.get("group") == "unauthorized":
                checks, observed = self._check_unauthorized(case, user)
            else:
                checks, observed = self._check_ticket_case(
                    case,
                    user=user,
                    tenant_id=tenant_id,
                    evaluation_key=evaluation_key or _evaluation_case_key("legacy", case["case_id"]),
                )
        except Exception as exc:  # 单条用例异常不该中断整轮评测
            error = f"{type(exc).__name__}: {exc}"
            checks = {"no_exception": False}

        latency = int((time.perf_counter() - started) * 1000)
        detail = {
            "case_id": case["case_id"],
            "group": case["group"],
            "subject": case["subject"],
            "order_id": case.get("order_id"),
            "expect": expect,
            "observed": observed,
            "checks": checks,
            "passed": all(checks.values()) if checks else False,
            "latency_ms": latency,
            "notes": case.get("notes"),
            "error": error,
        }
        return detail, latency, int(observed.get("tokens") or 0)

    def _check_unauthorized(self, case: dict, user: User) -> tuple[dict, dict]:
        """越权：请求越权知识库必须被拒绝（知识库级权限，走与问答同一条判定路径）。"""
        from app.services.knowledge_qa import QaPermissionError, answer_question

        requested = list(case.get("request_knowledge_bases") or [])
        observed: dict = {"requested_kbs": requested}
        try:
            answer_question(
                self.session,
                query=case["content"],
                tenant_id=user.tenant_id,
                user_id=user.id,
                # `User.role_codes` 是现成的属性；写成 `role.code for role in user.roles` 会 AttributeError
                roles=user.role_codes,
                clearance=user.security_level,
                allowed_kb_codes=[],
                requested_kb_codes=requested,
                use_model=False,
            )
            observed["error_code"] = None
            denied = False
        except QaPermissionError as exc:
            observed["error_code"] = exc.code
            denied = True
        checks = {
            "permission_denied": denied,
            "error_code_matches": observed.get("error_code") == (case["expect"].get("error_code") or "permission_denied"),
        }
        return checks, observed

    def _check_ticket_case(
        self, case: dict, *, user: User, tenant_id: str, evaluation_key: str
    ) -> tuple[dict, dict]:
        expect = case.get("expect") or {}
        service = TicketService(self.session)
        ticket, _ = service.create(
            tenant_id=tenant_id,
            actor_id=user.id,
            subject=case["subject"],
            content=case["content"],
            external_id=f"EVAL-{evaluation_key}",
            customer_id=case.get("customer_id"),
            order_id=case.get("order_id"),
            attributes=case.get("attributes") or {},
            idempotency_key=f"eval-{evaluation_key}",
        )

        observed: dict = {"ticket_id": ticket.id}
        analyze_status = 202
        missing: list[str] = []
        run_id: str | None = None
        try:
            run_id = start_analysis(self.session, ticket=ticket, user=user)
        except FactIncomplete as exc:
            analyze_status = 409
            missing = list(exc.missing)
        except Exception as exc:  # 其他异常按 500 处理并留痕
            analyze_status = 500
            observed["analyze_error"] = f"{type(exc).__name__}: {exc}"

        # 必须**等这次分析真的跑完**再判分（否则整轮评测全是假失败）。
        # 踩过的坑：界面点"运行售后工单评测"时，接口用的是默认的 threadpool 后端，
        # `start_analysis` 只是派发就返回 —— 立刻判分等于"什么都没产出"，
        # 8 条用例全部 0 分，而后台真的把 8 次分析跑完了：钱花了、分还是错的。
        # 离线脚本（inline）下这次等待是空操作，因此两条路径行为一致。
        waited = self._wait_for_task(run_id) if run_id else False
        observed["waited_for_completion"] = waited
        if run_id and not waited:
            observed["analyze_error"] = observed.get("analyze_error") or "分析未在超时内完成"

        self.session.expire_all()
        ticket = service.get(ticket.id)
        recommendation = service.latest_recommendation(ticket.id)
        runs = self._agent_runs(ticket.last_analysis_task_id)

        observed.update(
            {
                "analyze_status": analyze_status,
                "ticket_status": ticket.status,
                "issue_type": ticket.issue_type,
                "missing": missing,
                "missing_fields": list(ticket.missing_fields or []),
                "action_type": recommendation.action_type if recommendation else None,
                "amount": str(recommendation.amount) if recommendation and recommendation.amount is not None else None,
                "risk_tier": recommendation.risk_tier if recommendation else ticket.risk_tier,
                "approval_requirement": recommendation.approval_requirement if recommendation else None,
                "status": recommendation.status if recommendation else None,
                "policy_versions": sorted(
                    {str(ref.get("version")) for ref in (recommendation.policy_refs or []) if ref.get("version")}
                )
                if recommendation
                else [],
                "has_evidence": bool(recommendation and recommendation.evidence_ids),
                "has_policy": bool(recommendation and recommendation.policy_refs),
                "missing_information": list(recommendation.missing_information or []) if recommendation else [],
                "agent_count": len(runs),
                "tokens": sum(int(run.get("total_tokens") or 0) for run in runs),
                "usage_records": [
                    {
                        "model": run.get("model") or "stub",
                        "input_tokens": int(run.get("prompt_tokens") or 0),
                        "output_tokens": int(run.get("completion_tokens") or 0),
                        "cache_hit_tokens": int(run.get("cache_hit_tokens") or 0),
                        "model_used": bool(run.get("model_used")),
                    }
                    for run in runs
                ],
            }
        )

        checks = self._evaluate(case, observed, service, ticket, recommendation)
        return checks, observed

    def _wait_for_task(self, task_id: str, *, timeout_seconds: int = 300) -> bool:
        """阻塞等待一次分析运行到终态（跨会话读取，因为执行可能发生在别的线程/进程）。

        返回是否在超时前到达终态。评测**必须**用真实端到端结果判分，
        所以这里选择等待而不是"改成 inline 执行"——后者会把评测变成只测内核、
        绕过队列与 worker（而那正是生产里的形态）。
        """
        from app.models.workflow import AnalysisTask
        from app.services.ticket_observability import TERMINAL_TASK_STATUS

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            probe = self.session_factory()
            try:
                status = probe.query(AnalysisTask.status).filter(AnalysisTask.id == task_id).scalar()
            finally:
                probe.close()
            if status in TERMINAL_TASK_STATUS:
                return True
            if status is None:
                return False  # 任务行不见了（不该发生）：立刻失败，别白等 5 分钟
            time.sleep(0.2)
        return False

    def _agent_runs(self, task_id: str | None) -> list[dict]:
        if not task_id:
            return []
        from app.models.workflow import AgentRun

        rows = self.session.query(AgentRun).filter(AgentRun.task_id == task_id).all()
        return [
            {
                "agent_code": row.agent_code,
                "status": row.status,
                "model": row.model,
                "prompt_tokens": row.prompt_tokens,
                "completion_tokens": row.completion_tokens,
                "total_tokens": row.total_tokens,
                "cache_hit_tokens": int((row.output or {}).get("cache_hit_tokens") or 0),
                "model_used": bool((row.output or {}).get("model_used")),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------ 判定
    def _evaluate(
        self, case: dict, observed: dict, service: TicketService, ticket, recommendation
    ) -> dict[str, bool]:
        expect = case.get("expect") or {}
        group = case.get("group")
        checks: dict[str, bool] = {}

        # ① 分析入口：期望被拒绝的用例（缺单号/订单不存在/超时）
        if expect.get("analyze_status"):
            checks["analyze_status_matches"] = observed["analyze_status"] == expect["analyze_status"]
            if expect.get("ticket_status"):
                checks["ticket_status_matches"] = observed["ticket_status"] == expect["ticket_status"]
            if expect.get("missing"):
                checks["missing_fields_match"] = sorted(observed["missing"]) == sorted(expect["missing"])
            if expect.get("missing_contains"):
                checks["missing_mentions_reason"] = any(
                    expect["missing_contains"] in item
                    for item in (observed["missing"] or observed["missing_fields"])
                )
            if expect.get("error_code"):
                checks["error_code_matches"] = observed.get("error_code") == expect["error_code"]
            return checks

        # ② 分类
        if expect.get("issue_type"):
            checks["issue_type_matches"] = observed["issue_type"] == expect["issue_type"]

        # ③ 动作与金额
        if expect.get("action_type"):
            checks["action_type_matches"] = observed["action_type"] == expect["action_type"]
        if expect.get("amount") is not None:
            checks["amount_matches"] = (observed["amount"] or "0") == str(expect["amount"])

        # ④ 风险与审批门禁
        tier = observed.get("risk_tier")
        if expect.get("risk_tier"):
            checks["risk_tier_matches"] = tier == expect["risk_tier"]
        if expect.get("max_risk_tier"):
            checks["risk_within_limit"] = _tier_rank(tier) <= _tier_rank(expect["max_risk_tier"])
        if expect.get("min_risk_tier"):
            checks["risk_at_least"] = _tier_rank(tier) >= _tier_rank(expect["min_risk_tier"])
        if expect.get("approval_requirement"):
            checks["approval_requirement_matches"] = (
                observed["approval_requirement"] == expect["approval_requirement"]
            )
        if expect.get("requires_approval"):
            checks["gate_requires_approval"] = observed["status"] == "pending_approval"

        # ⑤ 政策：必须命中期望版本，且**不得**引用失效/未生效版本
        if expect.get("policy_version_included"):
            checks["policy_version_included"] = expect["policy_version_included"] in (
                observed["policy_versions"] or []
            )
        leaked = set(observed["policy_versions"] or []) & FORBIDDEN_POLICY_VERSIONS
        checks["no_expired_policy_cited"] = not leaked

        # ⑥ 证据支持：只有政策型动作才强制政策证据。
        # ask_customer 是"信息不足/事实冲突，先问客户"，escalate 是转人工，
        # 这两类本来就不该引用政策；按所有用例统一要求会把正确行为判错。
        policy_actions = {
            "reply_only",
            "refund",
            "return_goods",
            "exchange",
            "reship",
            "compensate",
            "coupon",
        }
        requires_policy = (
            expect.get("action_type") in policy_actions
            or bool(expect.get("policy_version_included"))
        )
        if requires_policy:
            checks["policy_required"] = True
            checks["has_policy_evidence"] = bool(observed["has_policy"])

        # ⑦ 重复审批幂等
        if expect.get("idempotent_decision") and recommendation is not None:
            checks["duplicate_decision_idempotent"] = self._check_duplicate_decision(
                service, recommendation
            )

        # ⑧ 低置信度：必须给出缺失信息，不能停在"自动可发"
        if group == "low_confidence":
            checks["declares_missing_info"] = bool(observed["missing_information"]) or observed[
                "action_type"
            ] in ("ask_customer", None)

        return checks

    def _check_duplicate_decision(self, service: TicketService, recommendation) -> bool:
        """同一幂等键重复审批：只允许一次状态迁移（重复写操作率必须为 0）。"""
        if recommendation.status != "pending_approval":
            return True
        first = service.decide(
            recommendation,
            decision="approve",
            actor_id=None,
            reason="评测：第一次审批",
            idempotency_key=f"eval-dup-{recommendation.id}",
        )
        status_after_first = self.session.get(type(first), first.id).status
        second = service.decide(
            recommendation,
            decision="approve",
            actor_id=None,
            reason="评测：重复审批",
            idempotency_key=f"eval-dup-{recommendation.id}",
        )
        return second.status == status_after_first and second.decision_reason == first.decision_reason

    # ------------------------------------------------------------------ 指标聚合
    def _aggregate(
        self,
        details: list[dict],
        latencies: list[int],
        tokens: list[int],
        *,
        dataset_metadata: dict | None = None,
    ) -> dict:
        def rate(items: list[dict], key: str) -> float | None:
            hits = [item for item in items if key in (item.get("checks") or {})]
            if not hits:
                return None
            return round(sum(1 for item in hits if item["checks"][key]) / len(hits), 4)

        by_group: dict[str, list[dict]] = defaultdict(list)
        for detail in details:
            by_group[detail["group"]].append(detail)

        macro_f1 = _macro_f1(details)
        approval_cases = [
            item
            for item in details
            if (item.get("expect") or {}).get("requires_approval")
        ]
        adapter_cases = [
            item
            for item in details
            if item["group"] in {"missing_order", "order_not_found", "adapter_timeout"}
        ]
        policy_cases = [
            item for item in details if (item.get("checks") or {}).get("policy_required")
        ]
        with_evidence = [
            item for item in policy_cases if (item.get("observed") or {}).get("has_policy")
        ]

        sorted_latency = sorted(latencies)
        p95 = (
            sorted_latency[min(len(sorted_latency) - 1, int(round(0.95 * (len(sorted_latency) - 1))))]
            if sorted_latency
            else 0
        )
        from app.services.cost import UsageRecord, compute_cost

        usage_records = [
            UsageRecord(
                model=str(record.get("model") or "stub"),
                input_tokens=int(record.get("input_tokens") or 0),
                output_tokens=int(record.get("output_tokens") or 0),
                cache_hit_tokens=int(record.get("cache_hit_tokens") or 0),
            )
            for detail in details
            for record in (detail.get("observed") or {}).get("usage_records", [])
            if record.get("model_used")
        ]
        cost = compute_cost(usage_records, self.settings)
        dataset_metadata = dataset_metadata or {"dataset_version": DATASET_VERSION}
        real_model = self.settings.llm_provider != "stub"
        external_holdout = dataset_metadata.get("dataset_kind") == "external_holdout"
        if real_model and external_holdout:
            note = "外部独立 holdout 真实模型基线；上线结论仍需结合真实业务验收"
        elif external_holdout:
            note = "外部独立 holdout 的 stub 回归结果，不代表真实模型生产效果"
        else:
            note = "内部生成回归集基线：数字用于回归对比，不得写成生产效果（需求书 §11）"
        return {
            **dataset_metadata,
            "case_count": len(details),
            "passed": sum(1 for item in details if item["passed"]),
            "pass_rate": round(sum(1 for item in details if item["passed"]) / len(details), 4)
            if details
            else 0.0,
            # 需求书 §9 的核心指标
            "issue_macro_f1": macro_f1,
            "action_accuracy": rate(details, "action_type_matches"),
            "risk_accuracy": rate(details, "risk_tier_matches"),
            "approval_gate_recall": rate(approval_cases, "gate_requires_approval"),
            "expired_policy_leak_rate": round(
                1 - (rate(details, "no_expired_policy_cited") or 0.0), 4
            ),
            "evidence_support_rate": round(len(with_evidence) / len(policy_cases), 4)
            if policy_cases
            else 1.0,
            "adapter_failure_handling_rate": rate(adapter_cases, "analyze_status_matches"),
            "tenant_isolation_rate": rate(by_group.get("unauthorized", []), "permission_denied"),
            "duplicate_write_blocked_rate": rate(
                by_group.get("duplicate_decision", []), "duplicate_decision_idempotent"
            ),
            "classification_accuracy": rate(details, "issue_type_matches"),
            "policy_version_recall": rate(details, "policy_version_included"),
            # 性能与成本（需求书 §8）
            "p95_latency_ms": p95,
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
            "total_tokens": sum(tokens),
            "avg_tokens": round(sum(tokens) / len(tokens), 1) if tokens else 0.0,
            "currency": cost.currency,
            "input_tokens": cost.input_tokens,
            "output_tokens": cost.output_tokens,
            "cache_hit_tokens": cost.cache_hit_tokens,
            "model_calls": cost.calls,
            "total_cost_amount": round(cost.amount, 6),
            "avg_cost_amount": round(cost.amount / len(details), 6) if details else 0.0,
            "per_model": cost.as_dict()["per_model"],
            "unpriced_models": cost.unpriced_models,
            "per_group": {
                group: {
                    "count": len(items),
                    "pass_rate": round(sum(1 for i in items if i["passed"]) / len(items), 4),
                }
                for group, items in sorted(by_group.items())
            },
            "note": note,
        }


def _tier_rank(tier: str | None) -> int:
    return {"L1": 1, "L2": 2, "L3": 3, "L4": 4}.get(str(tier), 0)


def _macro_f1(details: list[dict]) -> float | None:
    """问题分类的 Macro-F1（只在标注了期望类型的用例上计算）。"""
    pairs = [
        (item["expect"]["issue_type"], (item.get("observed") or {}).get("issue_type"))
        for item in details
        if (item.get("expect") or {}).get("issue_type")
    ]
    if not pairs:
        return None
    labels = sorted({expected for expected, _ in pairs} | {actual for _, actual in pairs if actual})
    scores: list[float] = []
    for label in labels:
        tp = sum(1 for expected, actual in pairs if expected == label and actual == label)
        fp = sum(1 for expected, actual in pairs if expected != label and actual == label)
        fn = sum(1 for expected, actual in pairs if expected == label and actual != label)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        scores.append(f1)
    return round(sum(scores) / len(scores), 4)


def _evaluation_case_key(run_id: str, case_id: str) -> str:
    """Stable within one run, unique across runs, and safe for external identifiers."""
    return hashlib.sha256(f"{run_id}\0{case_id}".encode("utf-8")).hexdigest()[:24]


def _case_cost(detail: dict, settings=None) -> float:
    """单条用例成本估算：按已计价模型的输入/输出单价折算（与 cost 服务同一口径）。"""
    from app.services.cost import UsageRecord, compute_cost

    observed = detail.get("observed") or {}
    records = [
        UsageRecord(
            model=str(item.get("model") or "stub"),
            input_tokens=int(item.get("input_tokens") or 0),
            output_tokens=int(item.get("output_tokens") or 0),
            cache_hit_tokens=int(item.get("cache_hit_tokens") or 0),
        )
        for item in observed.get("usage_records", [])
        if item.get("model_used")
    ]
    return round(compute_cost(records, settings).amount, 6)


__all__ = [
    "DATASET_VERSION",
    "FORBIDDEN_POLICY_VERSIONS",
    "TicketEvaluationService",
    "_case_cost",
    "_evaluation_case_key",
]
