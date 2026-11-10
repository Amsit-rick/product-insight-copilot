"""入口节点：任务校验 → 意图识别 → 受限执行计划。

这三个节点是纯代码（不是 Agent）：
- 权限与参数校验必须由代码裁决（设计文档 7.3 节最后一段）；
- 意图识别可以用规则 + 可选模型，但结果只影响"取数范围"，不影响权限；
- 执行计划是受限的：每个步骤只能指向一个 Agent、一组知识库和固定指标能力。
"""

from __future__ import annotations

from datetime import date, timedelta

from app.graph.runtime import NodeRuntime
from app.graph.state import AnalysisState, bump_visits
from app.metrics.service import SqlMetricService, pick_baseline_version
from app.models.enums import AgentCode, IntentCode, RiskLevel
from app.services.audit import record_audit
from app.services.intent import classify_intent_rules
from app.services.metric_access import domain_denials, visible_metrics

DEFAULT_METRICS = ("next_day_retention", "crash_rate")


# --------------------------------------------------------------------------- 校验
def validate_request(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    settings = runtime.settings
    with runtime.node_session(state, "validate_request") as ctx:
        ctx.check_cancel()
        ctx.emit("开始校验任务参数与数据权限")

        policy = _policy()
        user = _load_user(ctx)
        roles = user.role_codes if user else []
        max_rank = user.security_level if user else 1

        requested_kbs = list((state.get("filters") or {}).get("knowledge_bases") or [])
        allowed = _allowed_kbs(roles)
        if not allowed:
            reason = "当前角色没有被授权访问任何知识库"
            ctx.emit(reason, {"roles": roles}, level="error")
            return _halt(state, "permission_denied", reason, runtime=runtime, ctx=ctx)
        if requested_kbs:
            outside = sorted(set(requested_kbs) - allowed)
            if outside:
                reason = f"请求的知识库超出权限范围: {outside}"
                ctx.emit(reason, {"roles": roles, "allowed": sorted(allowed)}, level="error")
                _audit_denied(ctx, state, roles, outside)
                return _halt(state, "permission_denied", reason, runtime=runtime, ctx=ctx)
        kb_codes = sorted(set(requested_kbs) & allowed) or sorted(allowed)

        # 指标 / 数据集级密级：知识库权限拦不住"含营收的经营大盘"这类请求，
        # 这里用 access_policies 判定受限指标域，拒绝方式与知识库越权完全一致（permission_denied），
        # 保证"越权一律不执行、不产出报告"这条规则只有一种实现。
        denied_domains = _domain_denials(runtime, state, ctx, roles, max_rank)
        if denied_domains:
            reason = (
                "请求的指标超出当前角色的密级上限："
                + "；".join(item.reason for item in denied_domains)
            )
            ctx.emit(
                reason,
                {"roles": roles, "denied_domains": [item.resource_name for item in denied_domains]},
                level="error",
            )
            _audit_denied(ctx, state, roles, [item.resource_name for item in denied_domains])
            return _halt(state, "permission_denied", reason, runtime=runtime, ctx=ctx)

        errors: list[str] = []
        raw_filters = dict(state.get("filters") or {})
        injection = policy.detect_injection(state.get("query", ""))
        if injection:
            ctx.emit(
                f"问题文本命中提示词注入特征「{injection}」，已记录并将强制人工审核",
                {"pattern": injection},
                level="warn",
            )

        date_from = _as_date(raw_filters.get("date_from"))
        date_to = _as_date(raw_filters.get("date_to"))
        if date_from and date_to and date_from > date_to:
            errors.append(f"时间范围不合法：{date_from} 晚于 {date_to}")
        today = date.today()
        if date_from and date_from > today + timedelta(days=30):
            errors.append(f"时间范围超出可用数据窗口：{date_from} 尚未到达")
        if not errors and not state.get("query", "").strip():
            errors.append("问题描述为空")

        unknown_metrics = _unknown_metrics(runtime, state, raw_filters, roles, max_rank)
        metrics = [m for m in (raw_filters.get("metrics") or []) if m not in unknown_metrics]
        if not metrics:
            metrics = _available_metrics(runtime, state, roles, max_rank) or list(DEFAULT_METRICS)

        if errors:
            reason = "；".join(errors)
            ctx.emit(f"参数校验未通过：{reason}", {"errors": errors}, level="error")
            return _halt(state, "invalid_request", reason, runtime=runtime, ctx=ctx)

        filters = {
            **raw_filters,
            "knowledge_bases": kb_codes,
            "metrics": metrics,
        }
        ctx.emit(
            "校验通过",
            {
                "knowledge_bases": kb_codes,
                "metrics": metrics,
                "max_security_rank": max_rank,
                "roles": roles,
            },
        )
        return {
            "halt": False,
            "halt_reason": "",
            "injection": {"pattern": injection, "action": "escalate"} if injection else None,
            "force_human_review": bool(state.get("force_human_review")) or bool(injection),
            "kb_codes": kb_codes,
            "max_security_rank": max_rank,
            "roles": roles,
            "filters": filters,
            "warnings": [f"未知指标已忽略: {unknown_metrics}"] if unknown_metrics else [],
            "node_visits": bump_visits(state, "validate_request"),
        }


def route_after_validate(state: AnalysisState) -> str:
    return "halt" if state.get("halt") else "continue"


# --------------------------------------------------------------------------- 意图
def classify_intent(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    with runtime.node_session(state, "classify_intent") as ctx:
        ctx.check_cancel()
        intent, confidence, reason = classify_intent_rules(state.get("query", ""))
        # 模型可用时做一次受控复核；不可用则使用规则结果
        gateway = _gateway()
        profile = gateway.profile_for("intent_classifier", sample_key=state.get("task_id"))
        if profile.configured:
            payload, usage = gateway.chat_json(
                _intent_system_prompt(),
                f"问题：{state.get('query')}",
                fallback=lambda: {"intent": intent, "confidence": confidence, "reason": reason},
                max_tokens=200,
                profile=profile,
            )
            candidate = str(payload.get("intent") or intent)
            if candidate in {item.value for item in IntentCode}:
                intent = candidate
                confidence = float(payload.get("confidence") or confidence)
                reason = str(payload.get("reason") or reason) + ("（模型复核）" if not usage.stub else "")
        ctx.emit(f"意图识别：{intent}（置信度 {confidence:.2f}）", {"reason": reason})
        if ctx.task is not None:
            ctx.task.intent = intent
        return {
            "intent": intent,
            "intent_confidence": confidence,
            "intent_reason": reason,
            "node_visits": bump_visits(state, "classify_intent"),
        }


# --------------------------------------------------------------------------- 计划
def create_plan(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    filters = dict(state.get("filters") or {})
    intent = state.get("intent") or IntentCode.GENERAL_QA.value
    kb_codes = list(state.get("kb_codes") or [])
    metrics = list(filters.get("metrics") or DEFAULT_METRICS)

    with runtime.node_session(state, "create_plan") as ctx:
        ctx.check_cancel()
        baseline, release_date, versions = _resolve_version_context(runtime, state, metrics, filters)
        if baseline:
            filters["baseline_version"] = baseline
        if release_date:
            filters["release_date"] = release_date
        plan = _build_plan(intent, filters, kb_codes, metrics, baseline)
        if ctx.task is not None:
            # 计划与最终过滤条件落库，便于前端展示"这一步到底要做什么"
            ctx.task.plan = plan
            ctx.task.filters = filters
            ctx.session.flush()
        ctx.emit(
            f"生成受限执行计划：{len(plan)} 个步骤",
            {
                "intent": intent,
                "baseline_version": baseline,
                "release_date": release_date,
                "versions": versions,
                "steps": [
                    {"agent": item["agent"], "goal": item["goal"]} for item in plan
                ],
            },
        )
        return {
            "plan": plan,
            "filters": filters,
            "warnings": (
                [f"未找到版本 {filters.get('app_version')} 的指标数据，版本对比将无法进行"]
                if filters.get("app_version") and not versions
                else []
            ),
            "node_visits": bump_visits(state, "create_plan"),
        }


def _build_plan(
    intent: str,
    filters: dict,
    kb_codes: list[str],
    metrics: list[str],
    baseline: str | None,
) -> list[dict]:
    """按意图决定各 Agent 的目标、检索问法与指标查询（受控、可审计）。"""
    version = filters.get("app_version")
    platform = filters.get("platform")
    channel = filters.get("channel")
    device = filters.get("device")
    start = str(filters.get("date_from") or "")
    end = str(filters.get("date_to") or "")
    metric_queries: list[dict] = []
    # 用户在创建任务时声明的范围（平台/渠道/机型）必须原样带进指标查询，
    # 否则「iOS 低端机的崩溃率」这类问题会退化成"整个平台的崩溃率"而看不出缺口。
    base_params = {"start_date": start, "end_date": end}
    if platform:
        base_params["platform"] = platform
    if channel:
        base_params["channel"] = channel
    if device:
        base_params["device"] = device

    def _breakdown_params(dimension: str) -> dict:
        params = {**base_params, "metric_name": metric, "dimension": dimension}
        # 要按某个维度拆分时，该维度不能同时作为过滤条件
        params.pop(dimension, None)
        if version:
            params["app_version"] = version
        return params

    for metric in metrics:
        if version and baseline:
            metric_queries.append(
                {
                    "capability": "compare_versions",
                    "params": {
                        **base_params,
                        "metric_name": metric,
                        "baseline_version": baseline,
                        "target_version": version,
                        "release_date": filters.get("release_date"),
                    },
                }
            )
        metric_queries.append(
            {
                "capability": "get_trend",
                "params": {**base_params, "metric_name": metric, "app_version": version},
            }
        )
        metric_queries.append(
            {"capability": "breakdown", "params": _breakdown_params("channel")}
        )
        metric_queries.append({"capability": "breakdown", "params": _breakdown_params("device")})
        metric_queries.append(
            {
                "capability": "detect_anomaly",
                "params": {**base_params, "metric_name": metric, "method": "zscore", "threshold": 2.0},
            }
        )

    goals = {
        IntentCode.METRIC_ANOMALY.value: {
            AgentCode.METRIC.value: "确认指标下降的范围、幅度与受影响分群",
            AgentCode.RELEASE.value: "确认同期版本变更与监控结论",
            AgentCode.FEEDBACK.value: "确认用户可感知的故障主题",
            AgentCode.PRODUCT.value: "确认指标口径与预期产品行为",
        },
        IntentCode.VERSION_IMPACT.value: {
            AgentCode.RELEASE.value: "梳理该版本的全部技术变更与风险点",
            AgentCode.METRIC.value: "对比版本前后指标差异",
            AgentCode.FEEDBACK.value: "确认版本发布后新增的反馈主题",
            AgentCode.PRODUCT.value: "确认该版本的产品目标与验收口径",
        },
        IntentCode.FEEDBACK_CLUSTER.value: {
            AgentCode.FEEDBACK.value: "聚类反馈主题并给出代表样本",
            AgentCode.PRODUCT.value: "确认相关功能的产品规则",
            AgentCode.RELEASE.value: "确认相关模块的近期变更",
            AgentCode.METRIC.value: "用指标验证反馈规模是否可观测",
        },
        IntentCode.REQUIREMENT_PRIORITY.value: {
            AgentCode.FEEDBACK.value: "评估相关诉求的规模与强度",
            AgentCode.PRODUCT.value: "确认需求与现有规划的关系",
            AgentCode.METRIC.value: "评估需求对应的指标空间",
            AgentCode.RELEASE.value: "评估实现成本与近期变更冲突",
        },
        IntentCode.GENERAL_QA.value: {
            AgentCode.PRODUCT.value: "整理产品侧相关规则与决策",
            AgentCode.FEEDBACK.value: "整理相关用户反馈",
            AgentCode.RELEASE.value: "整理相关技术背景",
            AgentCode.METRIC.value: "补充可用的指标事实",
        },
    }[intent]

    plan: list[dict] = []
    step = 0
    for agent_code in (
        AgentCode.PRODUCT.value,
        AgentCode.FEEDBACK.value,
        AgentCode.RELEASE.value,
        AgentCode.METRIC.value,
    ):
        step += 1
        agent_kbs = sorted(set(kb_codes) & _agent_kbs(agent_code))
        item: dict = {
            "step": step,
            "agent": agent_code,
            "goal": goals.get(agent_code, ""),
            "knowledge_bases": agent_kbs,
            "app_version": version,
            "date_from": filters.get("date_from"),
            "date_to": filters.get("date_to"),
            "enabled": bool(agent_kbs) or agent_code == AgentCode.METRIC.value,
        }
        if agent_code == AgentCode.METRIC.value:
            item["metric_queries"] = metric_queries[:12]
        plan.append(item)
    return plan


# --------------------------------------------------------------------------- 私有工具
def _policy():
    from app.services.policy_engine import get_policy_engine

    return get_policy_engine()


def _gateway():
    from app.services.model_gateway import get_model_gateway

    return get_model_gateway()


def _intent_system_prompt() -> str:
    from app.agents.prompts import INTENT_CLASSIFY_PROMPT

    return INTENT_CLASSIFY_PROMPT


def _load_user(ctx):
    from app.models.identity import User

    user_id = ctx.state.get("user_id")
    return ctx.session.get(User, user_id) if user_id else None


def _allowed_kbs(roles: list[str]) -> set[str]:
    from app.auth.rbac import allowed_knowledge_bases

    return allowed_knowledge_bases(roles)


def _agent_kbs(agent_code: str) -> set[str]:
    from app.services.policy_engine import AGENT_KBS

    return set(AGENT_KBS.get(agent_code, set()))


def _unknown_metrics(
    runtime: NodeRuntime,
    state: AnalysisState,
    filters: dict,
    roles: list[str] | None = None,
    max_rank: int | None = None,
) -> list[str]:
    """请求里"不存在"或"当前角色无权读"的指标名。

    两类都从计划里剔掉，但理由不同：前者是数据问题（提示"未知指标已忽略"），
    后者是权限问题（走 metric_access 判定，不提示具体数值）。
    """
    requested = list(filters.get("metrics") or [])
    if not requested:
        return []
    session = runtime.session_factory()
    try:
        # 关键：**必须带上访问范围**构造服务。不带角色时，受策略保护的指标在
        # `list_metrics()` 里就已经被判为不可见，会被这里误判成"未知指标"而从计划里剔除，
        # 表现是"用户只是无权读某个指标，结果连'该指标数据缺失'都不再声明"。
        service = _metric_service(session, state, roles, max_rank)
        known = {item["name"] for item in service.list_metrics()}
        unknown = [m for m in requested if m not in known]
        visible = set(
            visible_metrics(
                session,
                tenant_id=state.get("tenant_id", "*"),
                roles=roles,
                user_id=state.get("user_id"),
                clearance=max_rank,
                metric_names=[m for m in requested if m in known],
            )
        )
        return unknown + [m for m in requested if m in known and m not in visible]
    except Exception:
        return []
    finally:
        session.close()


def _available_metrics(
    runtime: NodeRuntime,
    state: AnalysisState,
    roles: list[str] | None = None,
    max_rank: int | None = None,
) -> list[str]:
    session = runtime.session_factory()
    try:
        service = _metric_service(session, state, roles, max_rank)
        return service.list_metrics_covered()
    except Exception:
        return []
    finally:
        session.close()


def _metric_service(session, state: AnalysisState, roles=None, max_rank=None) -> SqlMetricService:
    """构建带上调用者访问范围的指标服务（密级判定与缓存键隔离都依赖它）。"""
    return SqlMetricService(
        session,
        tenant_id=state.get("tenant_id", "*"),
        roles=roles,
        user_id=state.get("user_id"),
        clearance=max_rank,
    )


def _domain_denials(
    runtime: NodeRuntime,
    state: AnalysisState,
    ctx,
    roles: list[str],
    max_rank: int,
):
    """问题文本命中受限指标域时的拒绝结果（无策略则返回空，保持默认放行）。"""
    session = runtime.session_factory()
    try:
        return domain_denials(
            session,
            tenant_id=state.get("tenant_id", "*"),
            roles=roles,
            user_id=state.get("user_id"),
            clearance=max_rank,
            query=state.get("query", ""),
        )
    except Exception as exc:  # 策略表查询失败不应把任务直接判失败，但必须留痕
        ctx.emit(f"指标密级策略查询失败（按默认放行处理）：{exc}", level="warn")
        return []
    finally:
        session.close()


def _resolve_version_context(
    runtime: NodeRuntime, state: AnalysisState, metrics: list[str], filters: dict
) -> tuple[str | None, str | None, list[dict]]:
    """挑出对比基线版本与发布日期（来自指标数据本身，而不是让模型猜）。"""
    session = runtime.session_factory()
    try:
        service = SqlMetricService(session, state.get("tenant_id", "*"))
        target = filters.get("app_version")
        versions: list[dict] = []
        for metric in metrics[:2]:
            versions = service.list_versions(
                metric, _as_date(filters.get("date_from")), _as_date(filters.get("date_to"))
            )
            if versions:
                break
        baseline = pick_baseline_version(versions, target)
        release_date = None
        target_info = next((v for v in versions if v.get("app_version") == target), None)
        if target_info:
            release_date = target_info.get("min_date")
            # 若基线在目标版本之后仍存在数据，说明版本号排序不等于发布时间，保守取目标最小日期
        return baseline, release_date, versions
    except Exception:
        return None, None, []
    finally:
        session.close()


def _as_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _halt(
    state: AnalysisState,
    code: str,
    reason: str,
    *,
    runtime: NodeRuntime,
    ctx,
) -> dict:
    """校验失败：不进入分析阶段，直接以明确原因结束并留痕。"""
    ctx.audit("task_validation_failed", outcome="rejected", detail={"code": code, "reason": reason})
    return {
        "halt": True,
        "halt_reason": reason,
        "branch": code,
        "risk_level": RiskLevel.MEDIUM.value,
        "errors": [{"node": "validate_request", "code": code, "message": reason}],
        "warnings": [],
        "node_visits": bump_visits(state, "validate_request"),
    }


def _audit_denied(ctx, state: AnalysisState, roles: list[str], outside: list[str]) -> None:
    """越权访问留痕。

    注意：必须复用**当前节点已有的事务**。SQLite 下如果在一个写事务未提交时
    另开一个连接去写，会直接 SQLITE_BUSY（写-写死锁，busy_timeout 不生效），
    这条审计记录反而会把整个任务打成失败。
    """
    record_audit(
        ctx.session,
        action="knowledge_base_access_denied",
        tenant_id=state.get("tenant_id", "*"),
        actor_id=state.get("user_id"),
        resource_type="knowledge_base",
        resource_id=",".join(outside)[:64],
        outcome="deny",
        detail={"roles": roles, "task_id": state.get("task_id")},
    )


__all__ = [
    "classify_intent",
    "create_plan",
    "route_after_validate",
    "validate_request",
]
