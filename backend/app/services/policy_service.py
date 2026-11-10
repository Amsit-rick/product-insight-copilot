"""售后政策服务：生效期与适用范围判定、政策创建与检索测试。

需求书 §4.3 要求检索必须过滤 `tenant_id、shop_id、policy_version、effective_at、permission_scope`。
这一层负责前三者与 effective_at，权限范围仍由既有的角色/密级逻辑把关。

**为什么"能不能用"要单独成一层**：政策有版本、有生效期、有适用范围。
把它塞进检索 SQL 里做条件判断，会出现"看不见的政策"（明明配置了却查不出来）和
"看得见的过期政策"（不该引用的却被引用）两类事故。所以这里统一给答案：

- `applicability(row, at, ...)` → (是否可用, 人话原因)：界面与接口都用同一份判定，
  检索层则用 `unusable_document_ids()` 做**排除**（推下推到向量库，不做事后过滤）。

适用范围匹配按**保守原则**：政策限定了某个维度而未提供该维度时判为"不适用"
（例：政策限店铺 A，工单没有店铺信息 → 不用它），因为"能引用"比"可能引用错"重要。
"""

from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy.orm import Session

from app.models.base import utcnow
from app.models.enums import PolicyStatus, SecurityLevel
from app.models.ticket import PolicyDocument
from app.services.audit import record_audit

# 这些取值表示"不限"（政策库里常见写法）
WILDCARDS = {"", "*", "all", "全平台", "全部", "不限", "any"}


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _matches(scope_value, actual: str | None, *, wildcard_ok: bool = True) -> bool:
    """范围值是否覆盖实际值。未限定（通配）→ 通过；限定了但实际值未知 → **不通过**。"""
    values = _as_list(scope_value)
    if not values or (wildcard_ok and all(item.lower() in WILDCARDS for item in values)):
        return True
    if actual is None or not str(actual).strip():
        return False  # 限定了范围但拿不到实际值：保守判为不适用
    actual_norm = str(actual).strip().lower()
    return any(item.lower() == actual_norm for item in values)


def applicability(
    row: PolicyDocument,
    *,
    at: datetime | None = None,
    shop_id: str | None = None,
    platform: str | None = None,
    member_level: str | None = None,
) -> tuple[bool, str]:
    """返回 (是否可用于当前工单, 人话原因)。"""
    now = at or utcnow()
    scope = dict(row.scope or {})

    if row.status == PolicyStatus.ARCHIVED.value:
        return False, "政策已归档"
    if row.effective_from and now < row.effective_from:
        return False, f"未生效（{row.effective_from:%Y-%m-%d} 起生效）"
    if row.effective_to and now > row.effective_to:
        return False, f"已失效（{row.effective_to:%Y-%m-%d} 失效）"
    if not _matches(scope.get("platform"), platform):
        return False, f"仅适用平台 {scope.get('platform')}，当前工单平台为 {platform or '未知'}"
    if not _matches(scope.get("shop_id"), shop_id):
        return False, f"仅适用店铺 {scope.get('shop_id')}，当前工单店铺为 {shop_id or '未知'}"
    if not _matches(scope.get("member_level"), member_level):
        return False, f"仅适用会员等级 {scope.get('member_level')}，当前工单为 {member_level or '未知'}"
    return True, "适用"


class PolicyService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ 查询
    def list_policies(
        self,
        *,
        tenant_id: str,
        at: datetime | None = None,
        shop_id: str | None = None,
        platform: str | None = None,
        member_level: str | None = None,
        kb_code: str | None = None,
    ) -> list[dict]:
        query = self.session.query(PolicyDocument).filter(PolicyDocument.tenant_id == tenant_id)
        if kb_code:
            query = query.filter(PolicyDocument.kb_code == kb_code)
        rows = query.order_by(PolicyDocument.title, PolicyDocument.effective_from.desc()).all()
        now = at or utcnow()
        result: list[dict] = []
        for row in rows:
            usable, reason = applicability(
                row,
                at=now,
                shop_id=shop_id,
                platform=platform,
                # 会员等级同样是适用范围维度：不传就会把"仅 gold 会员"的政策判成不适用
                member_level=member_level,
            )
            result.append(
                {
                    "id": row.id,
                    "title": row.title,
                    "version": row.version,
                    "scope": row.scope or {},
                    "effective_from": row.effective_from.isoformat() if row.effective_from else None,
                    "effective_to": row.effective_to.isoformat() if row.effective_to else None,
                    "status": row.status,
                    "state": self._state(row, now),
                    "usable": usable,
                    "reason": reason,
                    "document_id": row.document_id,
                    "kb_code": row.kb_code,
                    "source_uri": row.source_uri,
                    "note": row.note,
                }
            )
        return result

    @staticmethod
    def _state(row: PolicyDocument, at: datetime) -> str:
        if row.status == PolicyStatus.ARCHIVED.value:
            return "archived"
        if row.effective_from and at < row.effective_from:
            return "future"
        if row.effective_to and at > row.effective_to:
            return "expired"
        return "active"

    def unusable_document_ids(
        self,
        *,
        tenant_id: str,
        at: datetime | None = None,
        shop_id: str | None = None,
        platform: str | None = None,
        member_level: str | None = None,
    ) -> list[str]:
        """不可引用的政策文档 ID（用于**排除**：过期、未生效、范围不符、已归档）。"""
        now = at or utcnow()
        rows = (
            self.session.query(PolicyDocument)
            .filter(PolicyDocument.tenant_id == tenant_id)
            .all()
        )
        excluded: list[str] = []
        for row in rows:
            if not row.document_id:
                continue
            usable, _ = applicability(
                row, at=now, shop_id=shop_id, platform=platform, member_level=member_level
            )
            if not usable:
                excluded.append(row.document_id)
        return sorted(set(excluded))

    # ------------------------------------------------------------------ 创建
    def create(
        self,
        *,
        tenant_id: str,
        actor_id: str | None,
        title: str,
        version: str,
        content: str,
        effective_from: date | None = None,
        effective_to: date | None = None,
        scope: dict | None = None,
        kb_code: str = "policy_kb",
        user=None,
        note: str | None = None,
    ) -> PolicyDocument:
        """创建政策：内容走既有文档入库管道（切片 + 向量），这里只补"生效期与范围"。"""
        from app.services.document_service import DocumentService

        start = effective_from or utcnow().date()
        source_name = f"{title}{version}.md"
        DocumentService(self.session).ingest(
            tenant_id=tenant_id,
            user=user,
            kb_code=kb_code,
            filename=f"{title}-{version}.md",
            data=content.encode("utf-8"),
            name=f"{title} {version}",
            source_name=source_name,
            security_level=SecurityLevel.INTERNAL.value,
            document_version=version,
            department="售后服务中心",
            meta={"policy": True, "policy_version": version},
        )
        # `ingest` 返回结果摘要 dict；政策记录必须挂到文档行上（检索层按 document_id 排除），
        # 所以重新查一次拿 id —— 踩过：直接 .id 会 AttributeError
        from app.models.knowledge import Document

        document = (
            self.session.query(Document)
            .filter(Document.tenant_id == tenant_id)
            .filter(Document.source_name == source_name)
            .first()
        )
        if document is None:
            raise RuntimeError(f"政策文档入库失败: {source_name}")
        now = utcnow()
        status = PolicyStatus.ACTIVE.value
        if effective_to and effective_to < now.date():
            status = PolicyStatus.EXPIRED.value
        elif start > now.date():
            status = PolicyStatus.DRAFT.value
        row = PolicyDocument(
            tenant_id=tenant_id,
            title=title,
            version=version,
            scope=scope or {},
            effective_from=datetime.combine(start, time.min),
            effective_to=datetime.combine(effective_to, time.max) if effective_to else None,
            status=status,
            source_uri=f"upload://{source_name}",
            document_id=document.id,
            kb_code=kb_code,
            note=note,
        )
        self.session.add(row)
        record_audit(
            self.session,
            action="policy_created",
            tenant_id=tenant_id,
            actor_id=actor_id,
            resource_type="policy_document",
            resource_id=row.id,
            detail={
                "title": title,
                "version": version,
                "effective_from": start.isoformat(),
                "effective_to": effective_to.isoformat() if effective_to else None,
                "scope": scope or {},
                "status": status,
            },
        )
        self.session.commit()
        return row

    def set_status(
        self,
        row: PolicyDocument,
        status: str,
        *,
        actor_id: str | None,
        reason: str | None = None,
    ) -> PolicyDocument:
        """启用 / 归档政策。理由会写进审计（政策下线是合规动作，必须能追责到"为什么"）。"""
        row.status = status
        record_audit(
            self.session,
            action="policy_status_changed",
            tenant_id=row.tenant_id,
            actor_id=actor_id,
            resource_type="policy_document",
            resource_id=row.id,
            detail={"version": row.version, "status": status, "reason": reason},
        )
        self.session.commit()
        return row

    # ------------------------------------------------------------------ 检索测试
    def search_test(
        self,
        *,
        tenant_id: str,
        query: str,
        at: datetime | None = None,
        shop_id: str | None = None,
        platform: str | None = None,
        member_level: str | None = None,
        top_k: int = 5,
        kb_code: str = "policy_kb",
    ) -> dict:
        """政策检索测试（需求书 §7 知识库页的"检索测试"）：返回命中 + 每条的可用性判定。

        刻意**同时返回被排除的版本**（`excluded`）：这样使用者能一眼看出
        "为什么这条政策没被用上"（过期/未生效/范围不符），而不是以为检索坏了。
        """
        from app.rag.retrieval import RetrievalService

        now = at or utcnow()
        excluded_ids = self.unusable_document_ids(
            tenant_id=tenant_id,
            at=now,
            shop_id=shop_id,
            platform=platform,
            member_level=member_level,
        )
        service = RetrievalService()
        hits = service.search(
            self.session,
            query=query,
            tenant_id=tenant_id,
            kb_codes=[kb_code],
            shop_id=shop_id,
            platform=platform,
            member_level=member_level,
            policy_at=now,
            top_k=top_k,
        )
        by_document = {
            row.document_id: row
            for row in self.session.query(PolicyDocument)
            .filter(PolicyDocument.tenant_id == tenant_id)
            .all()
            if row.document_id
        }
        items = []
        for hit in hits:
            row = by_document.get(hit.document_id)
            usable, reason = (True, "非政策文档") if row is None else applicability(
                row,
                at=now,
                shop_id=shop_id,
                platform=platform,
                member_level=member_level,
            )
            items.append(
                {
                    "chunk_id": hit.chunk_id,
                    "document_id": hit.document_id,
                    "source_name": hit.source_name,
                    "section_title": hit.section_title,
                    "score": hit.relevance_score,
                    "snippet": (hit.content or "")[:200],
                    "policy_version": row.version if row else None,
                    "effective_from": row.effective_from.isoformat() if row and row.effective_from else None,
                    "effective_to": row.effective_to.isoformat() if row and row.effective_to else None,
                    "usable": usable,
                    "reason": reason,
                }
            )
        excluded = [
            item
            for item in self.list_policies(
                tenant_id=tenant_id,
                at=now,
                shop_id=shop_id,
                platform=platform,
                member_level=member_level,
                kb_code=kb_code,
            )
            if not item["usable"]
        ]
        return {
            "query": query,
            "at": now.isoformat(),
            "shop_id": shop_id,
            "platform": platform,
            "member_level": member_level,
            "hits": items,
            "excluded_policies": excluded,
            "excluded_document_ids": excluded_ids,
            "checked_at": now.isoformat(),
        }
