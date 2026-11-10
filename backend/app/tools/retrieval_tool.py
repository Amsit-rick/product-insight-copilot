"""检索工具：Agent 通过它访问知识库（唯一入口）。"""

from __future__ import annotations

from datetime import date, datetime

from app.rag.retrieval import get_retrieval_service
from app.tools.registry import ReplayPolicy, SideEffectLevel, ToolContext, ToolSpec


def _parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


class RetrievalSearchTool:
    """向量 + 全文混合召回，强制租户 / 密级 / 知识库三重过滤。"""

    name = "retrieval.search"

    def spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            description=(
                "在产品/反馈/研发/运营知识库中检索证据切片。"
                "返回可定位的引用（文档、页码、版本、相关度）。"
            ),
            handler=self.run,
            replay_policy=ReplayPolicy.SAFE,
            side_effect_level=SideEffectLevel.READ_ONLY,
            arg_schema={
                "query": "检索问题（自然语言）",
                "knowledge_bases": "知识库代码列表，缺省使用当前 Agent 的授权范围",
                "top_k": "返回条数，默认 8",
                "app_version": "限定版本",
                "date_from": "起始日期 YYYY-MM-DD",
                "date_to": "结束日期 YYYY-MM-DD",
                "shop_id": "工单所属店铺（政策适用范围过滤用）",
                "platform": "工单所属平台（政策适用范围过滤用）",
            },
        )

    def run(
        self,
        *,
        session,
        ctx: ToolContext,
        query: str,
        knowledge_bases: list[str] | None = None,
        top_k: int | None = None,
        app_version: str | None = None,
        date_from=None,
        date_to=None,
        shop_id: str | None = None,
        platform: str | None = None,
        **_ignored,
    ) -> dict:
        target_kbs = [kb for kb in (knowledge_bases or sorted(ctx.allowed_kb_codes))]
        if not target_kbs:
            return {"hits": [], "query": query, "knowledge_bases": [], "note": "无可用知识库"}

        hits = get_retrieval_service().search(
            session,
            query=query,
            tenant_id=ctx.tenant_id,
            kb_codes=target_kbs,
            max_security_rank=ctx.max_security_rank,
            top_k=top_k,
            app_version=app_version,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            # 政策生效期与适用范围过滤：不传也会按"现在 + 未知店铺"保守过滤
            shop_id=shop_id,
            platform=platform,
        )
        return {
            "query": query,
            "knowledge_bases": target_kbs,
            "hits": [hit.as_evidence() for hit in hits],
            "count": len(hits),
        }


def retrieval_search(**kwargs) -> dict:
    return RetrievalSearchTool().run(**kwargs)
