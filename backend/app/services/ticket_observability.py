"""工单成本与运行事件（SSE）。

需求书 §6 要求 `POST /tickets/{id}/analyze` 返回 `run_id`，前端通过
`GET /runs/{run_id}/events` 用 SSE 获取节点进度；§8 要求可观测 Token/延迟/成本。

三件事在这里落地：

1. **SSE 事件流**：把 `analysis_events` 表里已有的节点事件流式推给前端。
   事件数据本来就有（队列/worker/内嵌执行都会写），缺的只是"能推"的端点。
   轮询间隔与最大时长都可配 —— 不设上限的话，浏览器断连前会一直占着连接。
2. **每单成本**：一次分析 = 一个 `AnalysisTask`，所以"每单成本"就是该工单历次分析的合计。
   这也让"高频场景成本会失控"这件事**可被测量**，而不是只能凭感觉。
3. **成本闸门**：超过单日预算的工单分析会走降级（见 `services/limits.py`），
   这里的指标是给运维与老板看的那个数字。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from sqlalchemy.orm import Session

from app.models.ticket import Ticket
from app.models.workflow import AnalysisEvent, AnalysisTask
from app.services.cost import CostBreakdown, compute_cost, task_cost

# 终态：出现这些状态后事件流可以正常收尾，不必等超时
TERMINAL_TASK_STATUS = {"completed", "failed", "cancelled"}


def ticket_analysis_task_ids(session: Session, ticket_id: str) -> list[str]:
    """该工单历次分析的运行 ID（按时间正序）。"""
    rows = (
        session.query(AnalysisTask.id)
        .filter(AnalysisTask.filters["ticket_id"].as_string() == ticket_id)
        .order_by(AnalysisTask.created_at.asc())
        .all()
    )
    return [row[0] for row in rows]


def ticket_cost(session: Session, ticket: Ticket) -> dict:
    """工单累计成本（含每模型明细与未计价模型提示）。

    为什么不只用 `last_analysis_task_id`：一张工单可能被多次分析（改了信息重跑、审批驳回重分析），
    成本必须按**累计**口径看，否则会低估——评审一个"单次很便宜"的功能却上线到高频场景时最容易踩。
    """
    task_ids = ticket_analysis_task_ids(session, ticket.id)
    if ticket.last_analysis_task_id and ticket.last_analysis_task_id not in task_ids:
        task_ids.append(ticket.last_analysis_task_id)

    total = CostBreakdown(currency="CNY")
    per_task: list[dict] = []
    for task_id in task_ids:
        breakdown = task_cost(session, task_id)
        total.input_tokens += breakdown.input_tokens
        total.output_tokens += breakdown.output_tokens
        total.cache_hit_tokens += breakdown.cache_hit_tokens
        total.amount += breakdown.amount
        total.calls += breakdown.calls
        for model, item in (breakdown.per_model or {}).items():
            bucket = total.per_model.setdefault(
                model, {"input_tokens": 0, "output_tokens": 0, "amount": 0.0, "calls": 0}
            )
            for key in ("input_tokens", "output_tokens", "amount", "calls"):
                bucket[key] = bucket.get(key, 0) + item.get(key, 0)
        total.unpriced_models = sorted(set(total.unpriced_models) | set(breakdown.unpriced_models))
        per_task.append(
            {
                "task_id": task_id,
                "amount": round(breakdown.amount, 6),
                "total_tokens": breakdown.total_tokens,
                "calls": breakdown.calls,
                "cache_hit_tokens": breakdown.cache_hit_tokens,
            }
        )

    payload = total.as_dict()
    payload.update(
        {
            "ticket_id": ticket.id,
            "analysis_count": len(task_ids),
            "per_analysis": per_task,
            # 每单成本的最小可比口径：单次分析平均花费
            "amount_per_analysis": round(total.amount / len(task_ids), 6) if task_ids else 0.0,
        }
    )
    return payload


def estimate_batch_cost(per_ticket_amount: float, daily_volume: int) -> dict:
    """按单均成本推算批量场景开销（给"要不要用在高频场景"提供算术依据）。

    这个函数存在的理由很直接：本项目实测单次分析约 18.3k tokens / ¥0.13，
    放在每天几十次的调查场景里可忽略，放在每天上万单的售后场景里就是数量级问题。
    把算术封装起来，评审时可以直接引用数字而不是"感觉贵"。
    """
    daily = per_ticket_amount * max(daily_volume, 0)
    return {
        "per_ticket_amount": round(per_ticket_amount, 6),
        "daily_volume": daily_volume,
        "daily_amount": round(daily, 4),
        "monthly_amount": round(daily * 30, 2),
        "yearly_amount": round(daily * 365, 2),
    }


def event_stream(
    session_factory,
    run_id: str,
    *,
    poll_seconds: float = 0.5,
    max_seconds: float = 120.0,
    business_view: bool = False,
) -> Iterator[str]:
    """把一次分析的事件流做成 SSE 数据块。

    - 每个事件发一个 `event: <node>` + `data: <json>`；
    - 每次轮询用**新会话**：运行中的工作流在别的线程写事件，长连接持有的会话会读到快照；
    - 任务进入终态后发一个 `event: done` 并结束，避免前端一直等；
    - `max_seconds` 到点也正常收尾（断连由浏览器负责）。
    """
    last_id = None
    deadline = time.time() + max_seconds
    yield _sse(
        "open",
        {"message": "已连接处理进度"}
        if business_view
        else {"run_id": run_id, "message": "事件流已建立"},
    )

    while time.time() < deadline:
        session = session_factory()
        try:
            query = session.query(AnalysisEvent).filter(AnalysisEvent.task_id == run_id)
            if last_id:
                query = query.filter(AnalysisEvent.id != last_id)
            rows = query.order_by(AnalysisEvent.created_at.asc(), AnalysisEvent.id.asc()).all()
            task = session.get(AnalysisTask, run_id)
            status = task.status if task else None
        finally:
            session.close()

        if rows:
            last_id = rows[-1].id
        for row in rows:
            if business_view:
                message = _business_event_message(row.node)
                yield _sse(
                    "progress",
                    {
                        "message": message,
                        "at": row.created_at.isoformat() if row.created_at else None,
                    },
                )
                continue
            yield _sse(
                row.node or "event",
                {
                    "id": row.id,
                    "node": row.node,
                    "level": row.level,
                    "message": row.message,
                    "payload": row.payload or {},
                    "at": row.created_at.isoformat() if row.created_at else None,
                },
            )
        if status in TERMINAL_TASK_STATUS:
            yield _sse("done", {} if business_view else {"run_id": run_id, "status": status})
            return
        time.sleep(poll_seconds)

    yield _sse(
        "timeout",
        {"message": "进度暂未结束，请稍后查看工单状态"}
        if business_view
        else {"run_id": run_id, "message": f"事件流达到最长 {max_seconds:.0f} 秒，正常收尾"},
    )


def _business_event_message(node: str | None) -> str:
    return {
        "load_ticket_context": "正在读取工单和已核验事实",
        "triage_agent": "正在识别客户遇到的问题",
        "pass_triage": "已识别问题，开始核对订单事实和适用规则",
        "order_fact_agent": "正在核对订单、支付和物流记录",
        "policy_rag_agent": "正在核对适用的售后规则",
        "decide_action": "正在按业务规则确认处理范围",
        "resolution_agent": "正在整理处理建议",
        "pass_resolution": "处理建议已整理，开始准备回复和风险复核",
        "response_agent": "正在准备客户回复草稿",
        "review_agent": "正在复核证据和处理风险",
        "finalize_recommendation": "正在汇总核验结果",
        "followup_agent": "正在安排后续跟进",
        "persist_followup": "后续跟进安排已保存",
        "runner": "系统正在处理，请稍候",
    }.get(node or "", "正在核实相关信息")


def _sse(event: str, payload: dict) -> str:
    """SSE 帧格式。data 必须是单行 JSON（换行会破坏帧边界）。"""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


__all__ = [
    "TERMINAL_TASK_STATUS",
    "estimate_batch_cost",
    "event_stream",
    "ticket_analysis_task_ids",
    "ticket_cost",
]
