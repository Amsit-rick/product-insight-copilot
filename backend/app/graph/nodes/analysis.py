"""并行分析节点：四个专业 Agent + 输出校验。

图结构上这四个节点是**并行分支**，因此它们只能写带 reducer 的状态键：
`agent_results`（按 agent_code 合并）、`agent_evidence` / `agent_metric_results`（追加）、
`agent_errors`（追加）。

一个 Agent 失败不会影响其他 Agent：异常被收敛为失败结果写入 `agent_errors`，
其余分支照常产出，工作流继续（设计文档 16.3 第 6 条）。
"""

from __future__ import annotations

from app.agents import ANALYSIS_AGENTS, build_agent
from app.agents.schemas import AgentInput
from app.graph.runtime import NodeRuntime
from app.graph.state import AnalysisState, bump_visits
from app.logging_utils import get_logger

logger = get_logger(__name__)

AGENT_NODE_NAMES = {
    "product_agent": "product_agent",
    "feedback_agent": "feedback_agent",
    "release_agent": "release_agent",
    "metric_agent": "metric_agent",
}


def agent_node(agent_code: str):
    """生成一个 Agent 节点函数（供 builder 注册）。"""

    def node(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
        plan_item = _plan_item(state, agent_code)
        node_name = AGENT_NODE_NAMES.get(agent_code, agent_code)
        visits = bump_visits(state, node_name)

        if plan_item is not None and plan_item.get("enabled") is False:
            return {
                "agent_results": {
                    agent_code: {
                        "agent_code": agent_code,
                        "ok": True,
                        "skipped": True,
                        "summary": "该步骤在当前权限或数据范围内被跳过",
                        "findings": [],
                        "confidence": 0.0,
                        "data_gaps": ["没有可访问的知识库或指标范围"],
                    }
                },
                "warnings": [f"{agent_code} 因无可访问数据被跳过"],
                "node_visits": visits,
            }

        # short_writes=True：这个节点会把会话打开数秒（模型调用），写操作必须走短事务
        with runtime.node_session(state, node_name, short_writes=True) as ctx:
            ctx.check_cancel()
            kb_codes = set((plan_item or {}).get("knowledge_bases") or []) & set(
                state.get("kb_codes") or []
            )
            ai = AgentInput(
                session=ctx.session,
                task_id=state.get("task_id", ""),
                tenant_id=state.get("tenant_id", "*"),
                user_id=state.get("user_id", ""),
                query=state.get("query", ""),
                filters=dict(state.get("filters") or {}),
                intent=state.get("intent"),
                plan_item=plan_item or {},
                kb_codes=kb_codes,
                max_security_rank=int(state.get("max_security_rank") or 1),
                roles=list(state.get("roles") or []),
            )
            ctx.emit(
                f"{agent_code} 开始执行",
                {
                    "goal": (plan_item or {}).get("goal"),
                    "knowledge_bases": sorted(kb_codes),
                    "round": visits,
                },
            )
            agent = build_agent(agent_code)
            result = agent.run(ai)

            if result.ok:
                ctx.emit(
                    f"{agent_code} 完成：{len(result.findings)} 条候选结论，"
                    f"模型={'启用' if result.model_used else '未启用'}，耗时 {result.latency_ms}ms",
                    {
                        "evidence": len(result.evidence),
                        "metrics": len(result.metric_results),
                        "data_gaps": result.data_gaps[:5],
                        "tool_calls": result.tool_calls,
                    },
                    level="info" if result.schema_ok else "warn",
                )
            else:
                ctx.emit(
                    f"{agent_code} 未产出可用结论：{result.error}",
                    {"attempts": result.attempts},
                    level="error",
                )

            partial: dict = {
                "agent_results": {agent_code: result.as_state()},
                "agent_evidence": list(result.evidence),
                "agent_metric_results": list(result.metric_results),
                "node_visits": visits,
            }
            if not result.ok:
                partial["agent_errors"] = [
                    {
                        "agent_code": agent_code,
                        "error": result.error,
                        "attempts": result.attempts,
                    }
                ]
            return partial

    node.__name__ = f"{agent_code}_node"
    return node


def product_agent_node(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    return agent_node("product_agent")(state, runtime=runtime)


def feedback_agent_node(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    return agent_node("feedback_agent")(state, runtime=runtime)


def release_agent_node(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    return agent_node("release_agent")(state, runtime=runtime)


def metric_agent_node(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    return agent_node("metric_agent")(state, runtime=runtime)


# --------------------------------------------------------------------------- 校验
def validate_agent_outputs(state: AnalysisState, *, runtime: NodeRuntime) -> dict:
    """代码节点：对 Agent 输出做 Schema 与引用完整性复核（不信任 Agent 自述）。

    只写覆盖式键 + `agent_results`（按 code 合并，安全），
    证据过滤留给 `merge_evidence` 统一处理，避免 reducer 重复追加。
    """
    with runtime.node_session(state, "validate_agent_outputs", load_task=False) as ctx:
        results = dict(state.get("agent_results") or {})
        problems: list[str] = []
        sanitized: dict[str, dict] = {}

        for agent_code, payload in results.items():
            issues: list[str] = []
            if payload.get("skipped"):
                sanitized[agent_code] = payload
                continue
            if payload.get("schema_ok") is False:
                issues.extend(payload.get("schema_errors") or ["输出未通过 Schema 校验"])
            findings = payload.get("findings") or []
            if payload.get("ok") and not findings:
                issues.append("运行成功但没有产出任何候选结论")
            if not payload.get("ok"):
                issues.append(payload.get("error") or "Agent 运行失败")
            for finding in findings:
                if not finding.get("evidence_refs"):
                    issues.append(f"结论缺少证据引用：{str(finding.get('text'))[:40]}")
            if issues:
                problems.extend(f"[{agent_code}] {issue}" for issue in issues)
            sanitized[agent_code] = {**payload, "validated": not issues}

        expected = set(ANALYSIS_AGENTS)
        missing = sorted(expected - set(results))
        if missing:
            problems.append(f"以下 Agent 没有返回结果：{missing}")

        ctx.emit(
            f"Agent 输出校验完成：{len(results)} 个结果，{len(problems)} 处问题",
            {"problems": problems[:10]},
            level="warn" if problems else "info",
        )
        return {
            "agent_results": sanitized,
            "validation_problems": problems,
            "node_visits": bump_visits(state, "validate_agent_outputs"),
        }


def _plan_item(state: AnalysisState, agent_code: str) -> dict | None:
    for item in state.get("plan") or []:
        if item.get("agent") == agent_code:
            return item
    return None


__all__ = [
    "AGENT_NODE_NAMES",
    "agent_node",
    "feedback_agent_node",
    "metric_agent_node",
    "product_agent_node",
    "release_agent_node",
    "validate_agent_outputs",
]
