"""用户反馈 Agent：反馈主题聚类与代表样本提取。

职责边界：只访问 feedback_kb，不判断指标因果（因果交给指标 Agent 与证据校验环节）。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.base import AgentOutputModel, BaseAgent, Collected, make_evidence
from app.agents.schemas import AgentInput
from app.config import get_settings
from app.models.enums import KnowledgeBaseCode
from app.tools.registry import ToolContext

# 反馈聚类主题词表：第一版用可解释的规则聚类，后续可替换为向量聚类
THEME_KEYWORDS: dict[str, tuple[str, ...]] = {
    "登录与账号": ("登录", "登陆", "验证码", "账号", "token", "sdk"),
    "崩溃与闪退": ("闪退", "崩溃", "crash", "打不开", "无响应", "anr"),
    "性能与卡顿": ("卡顿", "加载慢", "耗时", "发热", "掉帧", "性能"),
    "内容与推荐": ("推荐", "内容", "信息流", "算法", "刷不出"),
    "支付与订单": ("支付", "订单", "退款", "扣款", "价格"),
    "通知与推送": ("推送", "通知", "消息", "提醒"),
    "体验与界面": ("界面", "ui", "引导", "改版", "找不到", "复杂"),
}

DEVICE_HINTS = ("华为", "小米", "oppo", "vivo", "荣耀", "低端机", "低端", "千元机", "红米")


class FeedbackThemeItem(BaseModel):
    theme: str = Field(max_length=120)
    sentiment: str = Field(default="negative", pattern="^(negative|neutral|positive)$")
    scale: str = Field(default="", max_length=200)
    first_seen: str | None = None
    quotes: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class FeedbackAgentOutput(AgentOutputModel):
    themes: list[FeedbackThemeItem] = Field(default_factory=list)


class FeedbackAgent(BaseAgent):
    code = "feedback_agent"
    display_name = "用户反馈 Agent"
    kb_scope = {KnowledgeBaseCode.FEEDBACK.value}
    output_model = FeedbackAgentOutput

    # ------------------------------------------------------------------ 取数
    def collect(self, ai: AgentInput, ctx: ToolContext, deadline: float) -> Collected:
        collected = Collected()
        queries = [
            ai.sub_query("用户反馈 工单 评论 问题"),
            ai.sub_query("登录 闪退 卡顿 差评"),
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
                collected.tool_errors.append(f"feedback_kb 检索失败: {exc}")
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
            collected.data_gaps.append("用户反馈知识库中没有检索到相关工单或评论")
        return collected

    # ------------------------------------------------------------------ 确定性聚类
    def deterministic_output(self, ai: AgentInput, collected: Collected) -> dict:
        buckets: dict[str, list[dict]] = {}
        for item in collected.evidence:
            content = (item.get("content") or "").lower()
            matched = False
            for theme, keywords in THEME_KEYWORDS.items():
                if any(k.lower() in content for k in keywords):
                    buckets.setdefault(theme, []).append(item)
                    matched = True
            if not matched:
                buckets.setdefault("其他反馈", []).append(item)

        themes: list[dict] = []
        findings: list[dict] = []
        for theme, items in sorted(buckets.items(), key=lambda kv: len(kv[1]), reverse=True):
            refs = [i["local_ref"] for i in items]
            quotes = [(i.get("content") or "").strip().replace("\n", " ")[:120] for i in items[:2]]
            devices = sorted(
                {
                    hint
                    for item in items
                    for hint in DEVICE_HINTS
                    if hint in (item.get("content") or "").lower()
                }
            )
            scale = f"命中 {len(items)} 条反馈切片"
            if devices:
                scale += f"，机型线索：{'/'.join(devices[:3])}"
            themes.append(
                {
                    "theme": theme,
                    "sentiment": "negative" if theme not in {"其他反馈"} else "neutral",
                    "scale": scale,
                    "first_seen": min(
                        (i.get("updated_at") for i in items if i.get("updated_at")), default=None
                    ),
                    "quotes": quotes,
                    "evidence_refs": refs,
                }
            )
            findings.append(
                {
                    "text": f"反馈主题「{theme}」：{scale}。代表反馈：{quotes[0] if quotes else '（无原文）'}",
                    "type": "fact",
                    "evidence_refs": refs,
                    "confidence": 0.72 if len(items) >= 2 else 0.58,
                }
            )

        if themes:
            top = themes[0]["theme"]
            findings.append(
                {
                    "text": f"反馈集中在「{top}」，共 {len(buckets)} 个主题；"
                    f"该主题与用户可感知的功能故障相关，需要与版本变更、指标表现交叉验证。",
                    "type": "inference",
                    "evidence_refs": themes[0]["evidence_refs"],
                    "confidence": 0.6,
                }
            )

        summary = (
            f"从 {len(collected.evidence)} 条反馈切片中聚出 {len(themes)} 个主题，"
            f"主题规模最大的是「{themes[0]['theme'] if themes else '无'}」。"
            if collected.evidence
            else "未检索到用户反馈材料，无法给出反馈侧证据。"
        )
        gaps = list(collected.data_gaps)
        if collected.evidence and not any(t["theme"] in {"崩溃与闪退", "登录与账号"} for t in themes):
            gaps.append("反馈材料未覆盖登录与崩溃相关主题，反馈侧证据不完整")
        return {"summary": summary, "findings": findings, "themes": themes, "data_gaps": gaps}
