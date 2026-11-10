"""评测接口（设计文档 16.2 节）：跑评测集、查看运行结果与用例。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import Principal, get_current_principal
from app.config import get_settings
from app.db.session import get_db
from app.models.evaluation import EvaluationCase, EvaluationRun
from app.schemas.evaluation import EvaluationCaseOut, EvaluationRunOut, EvaluationRunRequest
from app.services.evaluation_service import EvaluationService

router = APIRouter(prefix="/evaluations", tags=["评测"])

# 真实模型下"每条工单用例"的经验成本（元）：来自本机实测（7 个 Agent 约 5.8k~6.7k tokens/单）。
# 只用于**下单前的提示与确认**，不作为账单口径（账单看 agent_runs 的累计 token）。
DEFAULT_REAL_MODEL_COST_PER_CASE = 0.03


def real_model_enabled() -> bool:
    settings = get_settings()
    return settings.llm_provider != "stub" and bool(settings.llm_api_key)


def estimated_ticket_case_cost(session: Session) -> float:
    """估算每例成本：优先用历史评测实测均值，没有则退回经验常数。"""
    from sqlalchemy import select as _select

    row = session.execute(
        _select(EvaluationRun)
        .where(EvaluationRun.dataset_version.like("tickets%"))
        .order_by(EvaluationRun.created_at.desc())
        .limit(1)
    ).scalars().first()
    measured = float((row.metrics or {}).get("avg_cost_amount") or 0) if row is not None else 0.0
    return round(measured or DEFAULT_REAL_MODEL_COST_PER_CASE, 4)

METRIC_GUIDE = {
    "pass_rate": "全部用例整体通过率",
    "per_category": "按类别（单库/跨库/无答案/冲突/时间错配/数据缺失/越权/注入）的通过率",
    "refusal_accuracy": "无答案与越权用例的拒答/拒绝准确率",
    "conflict_detection_rate": "冲突证据用例的检出与转人工比例",
    "injection_handling_rate": "提示词注入用例的识别与升级比例",
    "check_pass_rate": "所有单项检查的通过率（更细粒度）",
    "human_takeover_rate": "需要人工接管的用例占比",
    "avg_latency_ms": "平均单用例耗时",
    "total_tokens": "本次评测的模型 Token 消耗",
}

# 售后工单评测的指标（需求书 §9）。为什么和诊断评测分开写：两张表说的是不同的事 ——
# 诊断看"证据是否可定位"，工单看"钱与风险有没有被管住"，混在一份说明里会让人把
# 审批门禁召回当成通过率的一部分（它比通过率重要得多）。
TICKET_METRIC_GUIDE = {
    "pass_rate": "全部工单用例整体通过率（每条用例的所有检查都通过才算）",
    "issue_macro_f1": "问题类型分类的 Macro-F1（每个类别同等权重，避免大类掩盖小类）",
    "classification_accuracy": "问题类型判对的用例占比",
    "action_accuracy": "处理动作（refund/reship/contact_logistics…）判对的占比",
    "risk_accuracy": "风险等级 L1~L4 判对的占比",
    "approval_gate_recall": "【安全】该进审批的是否都进了审批（应审批而未进审批 = 放出未授权赔付）",
    "expired_policy_leak_rate": "【安全】引用了已失效/未生效政策版本的比例（必须为 0，否则是合规事故）",
    "policy_version_recall": "期望的政策版本是否出现在依据里",
    "policy_version_included": "单条用例的政策版本命中检查",
    "evidence_support_rate": "建议带上了政策依据的用例占比（没有依据的建议不应直接使用）",
    "adapter_failure_handling_rate": "缺单号/订单不存在/接口超时三类异常是否被结构化处理（不许 500，也不许硬跑）",
    "tenant_isolation_rate": "【安全】越权访问知识库是否被拒绝",
    "duplicate_write_blocked_rate": "重复审批请求是否只产生一次状态迁移",
    "p95_latency_ms": "端到端 P95 耗时（单工单分析）",
    "avg_latency_ms": "端到端平均耗时",
    "total_tokens": "本轮评测的模型 Token 消耗（stub 下为 0）",
    "avg_tokens": "单均 Token",
    "avg_cost_amount": "单均成本（CNY）",
    "per_group": "按分组的通过率（常规/退款/风控/缺单号/越权…）",
}


@router.get("/metrics-guide", summary="评测指标说明")
def metrics_guide() -> dict:
    return {
        "metrics": METRIC_GUIDE,
        "categories": list(CASE_CATEGORIES),
        # 售后工单指标：需求书 §9。界面上不再需要靠猜字段含义（此前这一项是缺的）
        "ticket_metrics": TICKET_METRIC_GUIDE,
        "ticket_groups": TICKET_EVAL_GROUPS,
    }


TICKET_EVAL_GROUPS = (
    "normal",
    "refund",
    "risk_flag",
    "sensitive",
    "promotion",
    "damaged",
    "stalled",
    "stalled_short",
    "reship",
    "missing_order",
    "order_not_found",
    "adapter_timeout",
    "unauthorized",
    "duplicate_decision",
    "low_confidence",
    # 数据集扩到 200+ 条时新增的三个维度（评测口径没变，只是覆盖更宽）
    "cross_shop",  # 跨店/跨平台：多出来的属性文本不得污染分类、政策仍只引现行版本
    "fact_variant",  # 同一句话 × 不同订单事实：判定必须跟随事实而不是客户口述
    "escalation_wording",  # 二次追问/情绪化措辞：可以改变优先级，不得改变动作与风险口径
)


CASE_CATEGORIES = (
    "single_kb",
    "cross_kb",
    "no_answer",
    "conflict_evidence",
    "version_time_mismatch",
    "data_missing",
    "unauthorized",
    "prompt_injection",
)


@router.get("/cases", summary="评测集用例列表")
def list_cases(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    category: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
) -> dict:
    principal.require("task:read")
    query = session.query(EvaluationCase).filter(EvaluationCase.tenant_id == principal.tenant_id)
    if category:
        query = query.filter(EvaluationCase.category == category)
    cases = query.order_by(EvaluationCase.category, EvaluationCase.created_at).limit(limit).all()
    return {
        "items": [EvaluationCaseOut.model_validate(item) for item in cases],
        "total": len(cases),
    }


@router.post(
    "/run",
    response_model=EvaluationRunOut,
    status_code=status.HTTP_201_CREATED,
    summary="运行评测（真实跑工作流，同步返回结果）",
)
def run_evaluation(
    payload: EvaluationRunRequest,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> EvaluationRunOut:
    principal.require("eval:run")
    service = EvaluationService(session)
    run_row = service.run(
        tenant_id=principal.tenant_id,
        dataset_version=payload.dataset_version,
        name=payload.name,
        limit=payload.limit,
        categories=payload.categories,
    )
    return EvaluationRunOut.model_validate(run_row)


@router.post(
    "/tickets/run",
    response_model=EvaluationRunOut,
    status_code=status.HTTP_201_CREATED,
    summary="运行售后工单评测（需求书 §9）",
)
def run_ticket_evaluation(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    limit: int | None = Query(default=None, ge=1, le=500, description="只跑前 N 条（调试）"),
    groups: list[str] | None = Query(default=None, description="只跑指定分组"),
    name: str | None = Query(default=None),
    confirm_real_model: bool = Query(
        default=False,
        description="确认为真实模型计费付费（stub 配置下无需该参数）",
    ),
) -> EvaluationRunOut:
    """跑售后工单评测集（分类 Macro-F1 / 审批门禁召回 / 政策命中 / P95 / 每单成本…）。

    默认跑全量；调试时可 `?limit=6`。**注意**：该接口会真实执行工作流，
    stub 配置下零成本、真实模型配置下按用例数计费。
    """
    principal.require("eval:run")

    # 成本闸门：真实模型下一轮 64 条评测是要花钱的（经验值约 ¥0.03/例），
    # 所以必须由调用方显式确认。踩过的坑：界面点一下就把几百块量级的动作跑掉了，
    # 而按钮上没有任何成本提示 —— 与脚本里的 --allow-real-models 是同一条纪律。
    if real_model_enabled() and not confirm_real_model:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "real_model_confirmation_required",
                "message": (
                    f"当前模型为 {get_settings().llm_provider}，一轮评测会真实计费"
                    f"（约 ¥{estimated_ticket_case_cost(session):.2f}/例 × 用例数）。"
                    "确认后请带 confirm_real_model=true 重试；想免费复现基线请把 LLM_PROVIDER 设为 stub。"
                ),
                "estimated_cost_per_case": estimated_ticket_case_cost(session),
            },
        )

    from app.services.ticket_evaluation_service import TicketEvaluationService

    service = TicketEvaluationService(session)
    run_row = service.run(
        tenant_id=principal.tenant_id, name=name, limit=limit, groups=groups or None
    )
    return EvaluationRunOut.model_validate(run_row)


@router.get("/tickets/cases", summary="售后工单评测集用例")
def list_ticket_cases(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    principal.require("task:read")
    from app.services.ticket_evaluation_service import DATASET_VERSION, TicketEvaluationService

    cases = TicketEvaluationService.load_cases(limit=limit)
    settings = get_settings()
    real = real_model_enabled()
    per_case = estimated_ticket_case_cost(session) if real else 0.0
    return {
        "dataset_version": DATASET_VERSION,
        "total": len(cases),
        # 让界面在**点运行之前**就能提示"这次会不会花钱"（不然按钮是个盲盒）
        "llm_provider": settings.llm_provider,
        "real_model": real,
        "estimated_cost_per_case": per_case,
        "estimated_cost_total": round(per_case * len(cases), 4),
        "items": [
            {
                "case_id": case["case_id"],
                "group": case["group"],
                "subject": case["subject"],
                "order_id": case.get("order_id"),
                "expect": case.get("expect") or {},
                "notes": case.get("notes"),
            }
            for case in cases
        ],
    }


@router.get("/runs", response_model=list[EvaluationRunOut], summary="评测运行列表")
def list_runs(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[EvaluationRunOut]:
    principal.require("task:read")
    rows = (
        session.query(EvaluationRun)
        .filter(EvaluationRun.tenant_id == principal.tenant_id)
        .order_by(EvaluationRun.created_at.desc())
        .limit(limit)
        .all()
    )
    return [EvaluationRunOut.model_validate(item) for item in rows]


@router.get("/runs/{run_id}", response_model=EvaluationRunOut, summary="评测运行详情")
def get_run(
    run_id: str,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> EvaluationRunOut:
    principal.require("task:read")
    row = session.get(EvaluationRun, run_id)
    if row is None or row.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=404, detail="评测运行不存在")
    return EvaluationRunOut.model_validate(row)
