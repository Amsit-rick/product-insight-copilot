"""缓存键、指纹与命中统计（设计文档 §15 阶段六：缓存与降级）。

设计取舍——**为什么不需要显式失效**：
检索/指标缓存的 key 里带一个"数据指纹"（知识库文档数、切片数、最近索引时间；
指标导入批次数与最近导入时间）。指纹来自数据库状态，因此：

- 文档入库、重建索引、删除文档 → 指纹自动变化 → 旧缓存自然失效；
- 指标重新导入 → 指纹变化 → 旧缓存自然失效；
- 进程重启 / 换 Redis 实例也不会有"旧版本缓存"问题。

代价是每次查询要多做一次很小的聚合查询（knowledge_bases / metric_imports 都是小表），
换来的是不必维护"谁该清哪个前缀"的失效矩阵——这类代码最容易漏。

命中的语义：**缓存只用于省钱的路径**（检索与指标查询），不缓存模型输出——
模型输出与 prompt/版本强相关，缓存它会让"可追溯"变复杂。
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import deque
from collections.abc import Callable
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.logging_utils import get_logger
from app.models.knowledge import DocumentChunk, KnowledgeBase
from app.models.metrics import MetricImport, MetricSnapshot
from app.services.cache import get_cache

logger = get_logger(__name__)

_lock = threading.Lock()
_FIELDS = ("hits", "misses", "errors", "skipped")
# 按命名空间分别计数：一个总命中率说明不了问题。
# 冷启动跑一个新问题时检索必然 0% 命中（每个 Agent 问的都是新问题，这是预期），
# 而同一任务内多个 Agent 查同一指标时指标缓存命中率很高 —— 混在一起看只会误导。
_stats: dict[str, dict[str, int]] = {}
# 最近 N 次查找的命中情况：累计值会被"很久以前的一次冷启动"长期拖累，
# 滚动窗口才能反映"现在缓存到底管不管用"。
_recent: deque[tuple[str, bool]] = deque(maxlen=200)


def _namespace_of(key: str, fallback: str = "other") -> str:
    """从 `pic:<namespace>:<digest>` 里取出命名空间（没显式传时用）。"""
    parts = key.split(":", 2)
    return parts[1] if len(parts) >= 3 and parts[0] == "pic" else fallback


def note_lookup(namespace: str, hit: bool, *, count_error: bool = False) -> None:
    """登记一次缓存查找（供其它缓存实现复用同一套统计，如向量缓存）。"""
    _bump(namespace, "hits" if hit else "misses")
    if count_error:
        _bump(namespace, "errors")


def database_scope(session: Session) -> str:
    """数据库身份指纹。

    必须进缓存键：多个环境（本地/测试/预发）如果共用同一个 Redis，
    只按业务参数做 key 会互相串数据——测试库的结果被生产读到是非常危险的。
    这里只取"驱动+主机+端口+库名"，不含账号口令。
    """
    try:
        url = session.get_bind().url
        identity = f"{url.drivername}://{url.host}:{url.port}/{url.database}"
        return _digest([identity])
    except Exception:  # pragma: no cover - 拿不到引擎信息时退化为固定值
        return "unknown"


def kb_fingerprint(session: Session, tenant_id: str, kb_codes: list[str]) -> str:
    """知识库数据指纹：任何入库/重建/删除都会改变它。

    除了知识库行上的统计字段，还**直接统计切片表**（数量 + 最近更新时间）：
    这样即使有人绕过 DocumentService 直接写切片，缓存也会失效，
    而不是指望"所有写入路径都会顺手更新计数"。
    """
    if not kb_codes:
        return "none"
    summaries = (
        session.query(
            KnowledgeBase.code,
            KnowledgeBase.document_count,
            KnowledgeBase.chunk_count,
            KnowledgeBase.last_indexed_at,
        )
        .filter(KnowledgeBase.tenant_id == tenant_id)
        .filter(KnowledgeBase.code.in_(sorted(kb_codes)))
        .all()
    )
    chunks = (
        session.query(func.count(DocumentChunk.id), func.max(DocumentChunk.updated_at))
        .filter(DocumentChunk.tenant_id == tenant_id)
        .filter(DocumentChunk.kb_code.in_(sorted(kb_codes)))
        .first()
    )
    payload = [
        [
            [row[0], int(row[1] or 0), int(row[2] or 0), row[3].isoformat() if row[3] else None]
            for row in sorted(summaries)
        ],
        [int(chunks[0] or 0) if chunks else 0, chunks[1].isoformat() if chunks and chunks[1] else None],
    ]
    return _digest([database_scope(session), payload])


def metric_fingerprint(session: Session, tenant_id: str) -> str:
    """指标数据指纹：每次导入批次都会改变它。

    注意两点（都踩过）：
    1. `metric_imports` 与 `metric_snapshots` 不能直接 join，否则统计行数变成笛卡尔积；
    2. 不能只看行数：**行数相同但内容被改过**时必须失效，所以把
       `max(updated_at)` 也算进来（快照表有 onupdate 时间戳）。
    """
    imports = (
        session.query(func.count(MetricImport.id), func.max(MetricImport.created_at))
        .filter(MetricImport.tenant_id == tenant_id)
        .first()
    )
    snapshots = (
        session.query(
            func.count(MetricSnapshot.id),
            func.max(MetricSnapshot.updated_at),
        )
        .filter(MetricSnapshot.tenant_id == tenant_id)
        .first()
    )
    payload = [
        int(imports[0] or 0) if imports else 0,
        imports[1].isoformat() if imports and imports[1] else None,
        int(snapshots[0] or 0) if snapshots else 0,
        snapshots[1].isoformat() if snapshots and snapshots[1] else None,
    ]
    return _digest([database_scope(session), payload])


def _digest(payload: Any) -> str:
    return hashlib.sha1(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]


def make_key(namespace: str, parts: dict) -> str:
    return f"pic:{namespace}:{_digest(parts)}"


def cached_call(
    key: str, ttl: int, producer: Callable[[], Any], *, namespace: str | None = None
) -> tuple[Any, bool]:
    """带统计的缓存读取：返回 (值, 是否命中)。

    缓存故障绝不能影响主流程：读写异常一律当作 miss 继续执行。
    """
    ns = namespace or _namespace_of(key)
    if ttl <= 0:
        _bump(ns, "skipped")
        return producer(), False
    cache = get_cache()
    try:
        cached = cache.get(key)
        if cached is not None:
            _bump(ns, "hits")
            return cached, True
    except Exception as exc:  # pragma: no cover - 缓存后端异常
        _bump(ns, "errors")
        logger.warning("缓存读取失败，按未命中处理: %s", exc)
    value = producer()
    try:
        cache.set(key, value, ttl)
    except Exception as exc:  # pragma: no cover
        _bump(ns, "errors")
        logger.warning("缓存写入失败: %s", exc)
    _bump(ns, "misses")
    return value, False


def _bump(namespace: str, field: str) -> None:
    with _lock:
        bucket = _stats.setdefault(namespace, dict.fromkeys(_FIELDS, 0))
        bucket[field] = bucket.get(field, 0) + 1
        if field in ("hits", "misses"):
            _recent.append((namespace, field == "hits"))


def _rate(hits: int, total: int) -> float:
    return round(hits / total, 4) if total else 0.0


def cache_stats() -> dict:
    """累计统计 + 按命名空间细分 + 最近窗口。

    顶层字段保持与早期版本一致（`hits` / `misses` / `hit_rate` / ...），
    新增 `namespaces` 与 `recent`：前者回答"哪一层缓存有效"，后者回答"现在还有效吗"。
    """
    with _lock:
        per_namespace = {name: dict(bucket) for name, bucket in _stats.items()}
        recent = list(_recent)

    totals = dict.fromkeys(_FIELDS, 0)
    for bucket in per_namespace.values():
        for field in _FIELDS:
            totals[field] += bucket.get(field, 0)
    total_lookups = totals["hits"] + totals["misses"]

    namespaces: dict[str, dict] = {}
    for name, bucket in sorted(per_namespace.items()):
        lookups = bucket["hits"] + bucket["misses"]
        namespaces[name] = {
            "hits": bucket["hits"],
            "misses": bucket["misses"],
            "total_lookups": lookups,
            "hit_rate": _rate(bucket["hits"], lookups),
        }

    recent_hits = sum(1 for _, hit in recent if hit)
    return {
        **totals,
        "total_lookups": total_lookups,
        "hit_rate": _rate(totals["hits"], total_lookups),
        "namespaces": namespaces,
        "recent": {
            "window": len(recent),
            "hits": recent_hits,
            "hit_rate": _rate(recent_hits, len(recent)),
        },
    }


def reset_cache_stats() -> None:
    with _lock:
        _stats.clear()
        _recent.clear()
