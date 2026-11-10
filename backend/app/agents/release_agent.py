"""版本研发 Agent：关联版本变更、发布记录、故障复盘与技术文档。

职责边界：只访问 engineering_kb，不修改任何代码或配置。
本 Agent 的重要职责之一是**如实呈现技术侧结论**，即使它与反馈侧证据矛盾。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from app.agents.base import AgentOutputModel, BaseAgent, Collected, make_evidence
from app.agents.schemas import AgentInput
from app.config import get_settings
from app.models.enums import KnowledgeBaseCode
from app.tools.registry import ToolContext

CHANGE_KEYWORDS = ("升级", "新增", "修改", "调整", "优化", "重构", "回滚", "sdk", "接口", "灰度", "上线")
COMPONENT_KEYWORDS = ("登录", "启动", "首屏", "网络", "推送", "支付", "埋点", "缓存", "数据库")
INCIDENT_KEYWORDS = ("故障", "复盘", "告警", "事故", "崩溃率", "可用性", "监控")
NEGATION_KEYWORDS = ("未发现", "没有发现", "无异常", "未见", "正常", "未上升", "无上升", "未出现")
RISK_HIGH_HINTS = ("登录", "启动", "崩溃", "闪退", "sdk", "灰度")
# 各类抽取结果的条数上限：防止单个文档把结论列表淹没
MAX_CHANGES = 10
MAX_INCIDENTS = 6
MAX_NEGATIONS = 3
# 采集阶段放宽上限，最后再按"与风险话题的贴合度"挑选（否则先到的句子会挤掉关键结论）
MAX_NEGATION_CANDIDATES = 20
RISK_TOPICS = ("崩溃", "闪退", "留存", "登录", "启动", "失败", "卡顿", "性能")

NEGATION_THEN_TOPIC = re.compile(
    r"(未发现|未观测到|未见|未出现|未上升|无上升|无异常|没有发现|无明显|不存在)"
    r"[^。；;]{0,16}(" + "|".join(RISK_TOPICS) + r")"
)


def negation_relevance(text: str) -> int:
    """否定结论与风险话题越贴合，越应该被当作"技术侧反证"呈现。"""
    return 0 if NEGATION_THEN_TOPIC.search(text or "") else 1


class ReleaseChangeItem(BaseModel):
    change: str = Field(max_length=400)
    component: str | None = Field(default=None, max_length=64)
    risk: str = Field(default="medium", pattern="^(low|medium|high)$")
    evidence_refs: list[str] = Field(default_factory=list)


class IncidentItem(BaseModel):
    title: str = Field(max_length=200)
    conclusion: str = Field(default="", max_length=600)
    evidence_refs: list[str] = Field(default_factory=list)


class ReleaseAgentOutput(AgentOutputModel):
    changes: list[ReleaseChangeItem] = Field(default_factory=list)
    incidents: list[IncidentItem] = Field(default_factory=list)


class ReleaseAgent(BaseAgent):
    code = "release_agent"
    display_name = "版本研发 Agent"
    kb_scope = {KnowledgeBaseCode.ENGINEERING.value}
    output_model = ReleaseAgentOutput

    # ------------------------------------------------------------------ 取数
    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        collected = Collected()
        version = ai.app_version or ""
        queries = [
            ai.sub_query(f"{version} 版本变更 发布记录"),
            ai.sub_query("故障 复盘 监控 告警"),
        ]
        seen: set[str] = set()
        raw: list[dict] = []
        for query in queries:
            try:
                data = self.call_tool(
                    ctx,
                    "retrieval.search",
                    {
                        "query": query,
                        "knowledge_bases": sorted(self.kb_scope & ai.kb_codes),
                        "top_k": 8,
                        "app_version": ai.app_version,
                    },
                )
            except Exception as exc:
                collected.tool_errors.append(f"engineering_kb 检索失败: {exc}")
                continue
            for hit in data.get("hits") or []:
                chunk_id = hit.get("chunk_id")
                if chunk_id and chunk_id in seen:
                    continue
                if chunk_id:
                    seen.add(chunk_id)
                raw.append(hit)
            self._check_deadline(deadline)

        raw.sort(key=lambda h: h.get("relevance_score") or 0, reverse=True)
        collected.evidence = make_evidence(
            raw[: get_settings().retrieval_top_k + 2], agent_code=self.code
        )
        if not collected.evidence:
            collected.data_gaps.append("研发知识库中没有检索到该版本的变更或故障记录")
        return collected

    # ------------------------------------------------------------------ 确定性归纳
    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        changes: list[dict] = []
        incidents: list[dict] = []
        findings: list[dict] = []
        negations: list[dict] = []

        for item in collected.evidence:
            content = item.get("content") or ""
            ref = item["local_ref"]
            source = item.get("source_name") or "研发文档"
            matched_change = False
            for line in content.splitlines():
                stripped = line.strip(" -•·|*#\t")
                if len(stripped) < 6:
                    continue
                lowered = stripped.lower()
                # 变更条目本身有数量上限，但**否定性结论必须继续扫描**：
                # 它们往往是判断"技术侧与反馈侧是否矛盾"的关键证据
                if len(changes) < MAX_CHANGES and any(k in lowered for k in CHANGE_KEYWORDS):
                    component = next((c for c in COMPONENT_KEYWORDS if c in lowered), None)
                    risk = "high" if any(h in lowered for h in RISK_HIGH_HINTS) else "medium"
                    changes.append(
                        {
                            "change": stripped[:300],
                            "component": component,
                            "risk": risk,
                            "evidence_refs": [ref],
                        }
                    )
                    matched_change = True
                if len(incidents) < MAX_INCIDENTS and any(k in stripped for k in INCIDENT_KEYWORDS):
                    incidents.append(
                        {"title": stripped[:150], "conclusion": "", "evidence_refs": [ref]}
                    )
                if len(negations) < MAX_NEGATION_CANDIDATES and any(
                    k in stripped for k in NEGATION_KEYWORDS
                ):
                    # 只保留含否定结论的那一句，避免把整段复盘原文当成"结论"
                    for sentence in re.split(r"[。；;]\s*", stripped):
                        if any(k in sentence for k in NEGATION_KEYWORDS) and len(sentence) >= 8:
                            negations.append(
                                {"text": sentence.strip()[:300], "ref": ref, "source": source}
                            )
                            break

            if matched_change:
                findings.append(
                    {
                        "text": f"《{source}》记录了与{ai.app_version or '该版本'}相关的技术变更"
                        f"（最近一条：{changes[-1]['change'][:120]}）。",
                        "type": "fact",
                        "evidence_refs": [ref],
                        "confidence": 0.75,
                    }
                )

        if changes:
            high_risk = [c for c in changes if c["risk"] == "high"]
            findings.append(
                {
                    "text": f"共识别 {len(changes)} 项技术变更，其中 {len(high_risk)} 项涉及登录/启动等"
                    f"核心链路，属于高影响面变更，需要与指标和反馈对照验证。",
                    "type": "inference",
                    "evidence_refs": sorted({r for c in changes for r in c["evidence_refs"]}),
                    "confidence": 0.65,
                }
            )
        # 技术侧否定性结论优先级更高（直接决定"是否与反馈侧矛盾"）：
        # 先按与风险话题的贴合度排序，再取前 N 条
        negations.sort(key=lambda n: (negation_relevance(n["text"]), len(n["text"])))
        for neg in negations[:MAX_NEGATIONS]:
            findings.append(
                {
                    "text": f"技术侧结论（需与反馈侧交叉验证）：{neg['text']}（来源《{neg['source']}》）",
                    "type": "fact",
                    "evidence_refs": [neg["ref"]],
                    "confidence": 0.6,
                }
            )

        summary = (
            f"研发知识库命中 {len(collected.evidence)} 条切片，提取 {len(changes)} 项变更、"
            f"{len(incidents)} 条故障/监控记录。"
            if collected.evidence
            else "研发知识库未命中该版本的变更与故障记录。"
        )
        gaps = list(collected.data_gaps)
        if collected.evidence and not changes:
            gaps.append("研发文档中未明确列出该版本的技术变更清单")
        return {
            "summary": summary,
            "findings": findings,
            "changes": changes,
            "incidents": incidents,
            "data_gaps": gaps,
        }
