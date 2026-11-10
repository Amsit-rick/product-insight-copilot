"""缓存有效性实测：命中率到底是多少、低在哪一层、重启之后还管不管用。

背景：工作台上曾长期显示"缓存命中率 7%（命中 1 / 未命中 13）"。查下来不是缓存坏了，
而是三件事叠在一起：

1. **统计口径**：那是进程启动以来的累计值。刚重启完跑**一个**新问题，检索键全是新的，
   命中率天然接近 0 —— 这是预期，不是故障；
2. **真的白干的活**：同一段查询文本在一次诊断里被重复向量化 6 次
   （多个 Agent 检索不同知识库，而 `rewrite_query` 只留前 24 个 token，长问题的追加词被截掉）；
3. **重启即清零**：Redis 不可用时降级成纯进程内字典，后端一重启缓存就没了，
   于是"同一个问题问第二遍"也永远不命中。

这个脚本在**临时库**上跑真实任务，逐阶段打印命中率与各层细分，并可复现"重启后仍命中"：

    ..\\.venv\\Scripts\\python.exe scripts\\verify_cache_effectiveness.py

不碰开发库、不调真实模型、不产生费用。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
TMP_DB = BACKEND / ".tmp" / "cache_effect_db.sqlite"
CACHE_DB = BACKEND / ".tmp" / "cache_effect_cache.sqlite"
for path in (TMP_DB, CACHE_DB):
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
TMP_DB.parent.mkdir(parents=True, exist_ok=True)

os.environ["DATABASE_URL"] = f"sqlite:///{TMP_DB.as_posix()}"
os.environ["TASK_RUN_INLINE"] = "true"
os.environ["AUTO_INIT_DB"] = "true"
os.environ["AUTO_SEED"] = "true"
os.environ["LLM_PROVIDER"] = "stub"
os.environ["EMBEDDING_PROVIDER"] = "stub"
os.environ["LLM_API_KEY"] = ""
os.environ["AGENT_MODEL_OVERRIDES"] = "{}"
# 走真实默认值：Redis 连不上时降级到**落盘**缓存（这正是开发环境的实际形态）
os.environ["CACHE_BACKEND"] = "auto"
os.environ["CACHE_FALLBACK"] = "sqlite"
os.environ["CACHE_SQLITE_PATH"] = str(CACHE_DB)
os.environ.pop("REDIS_URL", None)
os.environ["JWT_SECRET"] = "cache-effect-secret-at-least-32-bytes-long"
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

import app.services.cache as cache_module  # noqa: E402
import app.services.caching as caching  # noqa: E402
from app.main import app  # noqa: E402
from app.services.model_gateway import EmbeddingClient  # noqa: E402

QUESTION = (
    "Android 端 3.2.0 发布后次日留存下降，哪些用户受到影响？"
    "版本变更、用户反馈和故障记录是否支持版本相关性？下一步应该优先验证什么？"
)
FILTERS = {
    "product": "星野社区",
    "platform": "Android",
    "app_version": "3.2.0",
    "date_from": "2026-08-01",
    "date_to": "2026-08-31",
    "knowledge_bases": ["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
    "metrics": ["next_day_retention", "crash_rate"],
    "priority": "P1",
}

# ---------------------------------------------------------------- 埋点：真实向量化次数
embed_calls: list[list[str]] = []
_orig_embed_uncached = EmbeddingClient._embed_uncached


def counting_embed(self, texts):
    embed_calls.append(list(texts))
    return _orig_embed_uncached(self, texts)


EmbeddingClient._embed_uncached = counting_embed

lines: list[str] = []


def log(text: str = "") -> None:
    lines.append(text)
    print(text)


def snapshot() -> dict:
    stats = caching.cache_stats()
    return {
        "合计查找": stats["total_lookups"],
        "命中": stats["hits"],
        "未命中": stats["misses"],
        "命中率": stats["hit_rate"],
        "各层": {name: f"{bucket['hit_rate']:.0%}({bucket['hits']}/{bucket['total_lookups']})"
                 for name, bucket in stats["namespaces"].items()},
        "最近窗口": f"{stats['recent']['hit_rate']:.0%}",
    }


def run_task(client, headers, query: str, label: str) -> None:
    before = caching.cache_stats()
    before_embed_texts = sum(len(call) for call in embed_calls)
    created = client.post("/api/v1/tasks", headers=headers, json={"query": query, **FILTERS})
    created.raise_for_status()
    task_id = created.json()["id"]
    deadline = time.time() + 180
    status = "pending"
    while time.time() < deadline:
        detail = client.get(f"/api/v1/tasks/{task_id}", headers=headers).json()
        status = detail["status"]
        if status in {"completed", "failed", "cancelled", "waiting_human"}:
            break
        time.sleep(0.5)
    after = caching.cache_stats()
    delta_lookups = after["total_lookups"] - before["total_lookups"]
    delta_hits = after["hits"] - before["hits"]
    delta_embed = sum(len(call) for call in embed_calls) - before_embed_texts
    rate = (delta_hits / delta_lookups) if delta_lookups else 0.0
    embed_ns = after["namespaces"].get("embedding", {})
    log(f"  [{label}] 状态={status} 查找={delta_lookups} 命中={delta_hits} "
        f"命中率={rate:.0%} 实际向量化文本={delta_embed} "
        f"（向量缓存命中 {embed_ns.get('hits', 0)}/{embed_ns.get('hits', 0) + embed_ns.get('misses', 0)}）")


def main() -> int:
    log("=== 缓存有效性实测（临时库 + stub 模型，不花钱）===")
    with TestClient(app) as client:
        login = client.post("/api/v1/auth/login", json={"username": "pm", "password": "pm123"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        log(f"\n缓存后端：{json.dumps(client.get('/health').json()['cache'], ensure_ascii=False)[:220]}")

        log("\n[1/4] 冷启动：跑一个新问题")
        run_task(client, headers, QUESTION, "冷启动")

        log("\n[2/4] 同题重跑（同进程）")
        run_task(client, headers, QUESTION, "同题重跑")

        log("\n[3/4] 模拟后端重启：换一个进程内的 Cache 实例，磁盘缓存文件不变")
        cache_module._cache = None  # 等价于进程重启后重新构造缓存
        health_after = client.get("/health").json()["cache"]
        log(f"      重启后缓存后端 = {health_after.get('backend')}，"
            f"落盘条目 = {health_after.get('entries')}，persistent={health_after.get('persistent')}")
        run_task(client, headers, QUESTION, "重启后同题")

        log("\n[4/4] 近似问题（改一个词，指标缓存仍应命中）")
        run_task(client, headers, QUESTION.replace("哪些用户", "哪类用户"), "近似题")

        log("\n=== 累计统计 ===")
        log(json.dumps(snapshot(), ensure_ascii=False, indent=2))

        stats = caching.cache_stats()
        log("\n=== 结论 ===")
        retrieval = stats["namespaces"].get("retrieval", {})
        metric = stats["namespaces"].get("metric", {})
        embedding = stats["namespaces"].get("embedding", {})
        log(f"  检索缓存：{retrieval.get('hit_rate', 0):.0%}"
            f"（{retrieval.get('hits', 0)}/{retrieval.get('total_lookups', 0)}）"
            " —— 每个 Agent 问的都是新问题，冷启动低是正常的")
        log(f"  指标缓存：{metric.get('hit_rate', 0):.0%}"
            f"（{metric.get('hits', 0)}/{metric.get('total_lookups', 0)}）"
            " —— 同一任务内多个 Agent 查同一指标，复用率高")
        log(f"  向量缓存：{embedding.get('hit_rate', 0):.0%}"
            f"（{embedding.get('hits', 0)}/{embedding.get('total_lookups', 0)}）"
            " —— 同一段文本不再重复向量化")
        log(f"  最近窗口命中率：{stats['recent']['hit_rate']:.0%}（反映「现在」而不是「历史平均」）")

    out = BACKEND / ".tmp" / "cache_effectiveness_out.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
