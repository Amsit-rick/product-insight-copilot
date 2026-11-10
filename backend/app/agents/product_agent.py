"""产品知识 Agent：从 product_kb 提取产品规则、需求与历史决策。

职责边界（设计文档 7.3 节）：只访问 product_kb，不访问运营数据，不判断指标因果。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.base import AgentOutputModel, BaseAgent, Collected, make_evidence
from app.agents.schemas import AgentInput
from app.config import get_settings
from app.models.enums import KnowledgeBaseCode
from app.tools.registry import ToolContext

RULE_KEYWORDS = ("规则", "必须", "不支持", "禁止", "口径", "验收", "埋点", "需求", "决策", "优先级", "会议")
DECISION_KEYWORDS = ("决定", "结论", "评审", "通过", "暂缓", "排期")


class ProductRuleItem(BaseModel):
    rule: str = Field(max_length=800)
    evidence_refs: list[str] = Field(default_factory=list)


class ProductAgentOutput(AgentOutputModel):
    product_rules: list[ProductRuleItem] = Field(default_factory=list)


class ProductAgent(BaseAgent):
    code = "product_agent"
    display_name = "产品知识 Agent"
    kb_scope = {KnowledgeBaseCode.PRODUCT.value}
    output_model = ProductAgentOutput

    # ------------------------------------------------------------------ 取数
    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        collected = Collected()
        queries = [
            ai.sub_query("产品规则 埋点口径 验收标准"),
            ai.sub_query("需求 决策 版本"),
        ]
        seen: set[str] = set()
        raw_hits: list[dict] = []
        for query in queries:
            try:
                data = self.call_tool(
                    ctx,
                    "retrieval.search",
                    {
                        "query": query,
                        "knowledge_bases": sorted(self.kb_scope & ai.kb_codes),
                        "top_k": 6,
                        "app_version": ai.app_version,
                    },
                )
            except Exception as exc:
                collected.tool_errors.append(f"product_kb 检索失败: {exc}")
                continue
            for hit in data.get("hits") or []:
                chunk_id = hit.get("chunk_id")
                if chunk_id and chunk_id in seen:
                    continue
                if chunk_id:
                    seen.add(chunk_id)
                raw_hits.append(hit)
            self._check_deadline(deadline)

        raw_hits.sort(key=lambda h: h.get("relevance_score") or 0, reverse=True)
        collected.evidence = make_evidence(
            raw_hits[: get_settings().retrieval_top_k], agent_code=self.code
        )
        if not collected.evidence:
            collected.data_gaps.append("产品知识库中没有检索到与该问题相关的文档")
        return collected

    # ------------------------------------------------------------------ 确定性归纳
    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        rules: list[dict] = []
        findings: list[dict] = []
        by_doc: dict[str, list[dict]] = {}
        for item in collected.evidence:
            by_doc.setdefault(item.get("source_name") or "未命名文档", []).append(item)

        for index, (source, items) in enumerate(by_doc.items(), start=1):
            refs = [i["local_ref"] for i in items]
            section = next((i.get("section_title") for i in items if i.get("section_title")), None)
            excerpt = (items[0].get("content") or "")[:180].replace("\n", " ")
            findings.append(
                {
                    "text": f"产品文档《{source}》{f'（{section}）' if section else ''}中与问题直接相关的内容：{excerpt}",
                    "type": "fact",
                    "evidence_refs": refs,
                    "confidence": 0.7 if index <= 3 else 0.55,
                }
            )

        for item in collected.evidence:
            content = item.get("content") or ""
            for line in content.splitlines():
                stripped = line.strip(" -•·\t")
                if len(stripped) < 6:
                    continue
                if any(k in stripped for k in RULE_KEYWORDS):
                    rules.append({"rule": stripped[:300], "evidence_refs": [item["local_ref"]]})
                if len(rules) >= 8:
                    break
            if len(rules) >= 8:
                break

        version = ai.app_version or "当前版本"
        if rules:
            findings.append(
                {
                    "text": f"梳理出 {len(rules)} 条与{version}相关的产品规则/口径约束，可作为判断"
                    f"「是否符合预期设计」的依据。",
                    "type": "inference",
                    "evidence_refs": sorted({r for rule in rules for r in rule["evidence_refs"]}),
                    "confidence": 0.62,
                }
            )

        decisions = [r for r in rules if any(k in r["rule"] for k in DECISION_KEYWORDS)]
        summary = (
            f"产品知识库命中 {len(collected.evidence)} 条切片，覆盖 {len(by_doc)} 份文档，"
            f"提取 {len(rules)} 条规则、{len(decisions)} 条历史决策。"
            if collected.evidence
            else "产品知识库未命中相关文档，无法给出产品侧依据。"
        )

        gaps = list(collected.data_gaps)
        if not decisions:
            gaps.append("未检索到与本次问题直接相关的历史决策记录")

        return {
            "summary": summary,
            "findings": findings,
            "product_rules": rules,
            "data_gaps": gaps,
        }
