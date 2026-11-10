"""先量后改：语义缓存到底能捞回多少命中、代价（结果不一致）有多大。

背景：上一轮修掉"同段文本重复向量化"后，**检索层**命中率依然偏低。原因是结构性的：
每个 Agent 用"原问题 + 自己的关键词"检索，键天然不同。想再提高只剩两条路：

- **改写后精确复用**：把"不同原问题、但改写后完全相同"的检索并成一次；
- **近似（语义）复用**：把"向量足够接近"的检索并成一次 —— 但这是**近似**，
  复用的是别人那次查询的候选集，必须量化"结果会差多少"。

这个脚本用真实评测集的 50 个问题 + 模拟的真实使用形态（同一问题再问一遍、
换个说法再问、Agent 加关键词变体）作为负载，直接算出三套方案各自的命中率，
以及近似方案下"复用的候选集与精确候选集的重叠度"。**不调模型、不花钱、不碰开发库。**

    ..\\.venv\\Scripts\\python.exe scripts\\probe_semantic_cache_value.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
TMP_DB = BACKEND / ".tmp" / "semantic_probe.sqlite"
for suffix in ("", "-wal", "-shm"):
    candidate = Path(str(TMP_DB) + suffix)
    if candidate.exists():
        candidate.unlink()
TMP_DB.parent.mkdir(parents=True, exist_ok=True)

os.environ["DATABASE_URL"] = f"sqlite:///{TMP_DB.as_posix()}"
os.environ["AUTO_INIT_DB"] = "true"
os.environ["AUTO_SEED"] = "true"
os.environ["LLM_PROVIDER"] = "stub"
os.environ["EMBEDDING_PROVIDER"] = "stub"
os.environ["LLM_API_KEY"] = ""
os.environ["CACHE_BACKEND"] = "memory"
os.environ["JWT_SECRET"] = "semantic-probe-secret-at-least-32-bytes"
sys.path.insert(0, str(BACKEND))

from app.db.seed import init_db, seed_all  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.evaluation import EvaluationCase  # noqa: E402
from app.models.identity import Tenant  # noqa: E402
from app.rag.retrieval import RetrievalService  # noqa: E402
from app.services.caching import cache_stats, reset_cache_stats  # noqa: E402
from app.services.model_gateway import EmbeddingClient, cosine_similarity  # noqa: E402

# 直接建表 + 播种（脚本不走 app 启动流程，不能指望 lifespan 帮忙初始化）
init_db()
seed_all()

# 典型的知识库范围：四种 Agent 各自的授权范围（与工作流里一致）
AGENT_SCOPES = {
    "product_agent": ["product_kb"],
    "feedback_agent": ["feedback_kb"],
    "release_agent": ["engineering_kb"],
    "metric_agent": ["operation_kb"],
}
AGENT_SUFFIX = {
    "product_agent": "需求 规则 埋点",
    "feedback_agent": "工单 评论 反馈",
    "release_agent": "发布 变更 故障",
    "metric_agent": "指标 口径 趋势",
}


def build_workload(questions: list[str]) -> list[dict]:
    """构造贴近真实的使用形态：每个问题会被多个 Agent 以不同范围检索，
    并且会被用户用"原样再问一次 / 换个说法再问"的方式重复提交。"""
    workload: list[dict] = []
    for index, question in enumerate(questions):
        for agent, scope in AGENT_SCOPES.items():
            workload.append(
                {
                    "query": f"{question} {AGENT_SUFFIX[agent]}",
                    "kbs": scope,
                    "origin": f"agent:{agent}",
                    "case": index,
                }
            )
        # 同一问题原样再问（用户在界面上再点一次）
        workload.append({"query": question, "kbs": ["product_kb"], "origin": "repeat", "case": index})
        # 换个说法再问（多/少一个"的"、语序变化）
        variant = question.replace("是不是", "是否").replace("哪些", "哪类").replace("怎么", "如何")
        if variant != question:
            workload.append({"query": variant, "kbs": ["product_kb"], "origin": "rephrase", "case": index})
    return workload


def main() -> int:
    embedder = EmbeddingClient()
    service = RetrievalService()
    lines: list[str] = []

    def log(text: str = "") -> None:
        lines.append(text)
        print(text)

    with SessionLocal() as session:
        tenant = session.query(Tenant).first()
        if tenant is None:
            log("没有租户数据（seed 失败？）")
            return 1
        tenant_id = tenant.id
        cases = (
            session.query(EvaluationCase)
            .filter(EvaluationCase.tenant_id == tenant_id)
            .order_by(EvaluationCase.category, EvaluationCase.question)
            .all()
        )
        questions = [case.question for case in cases][:50]

        log(f"=== 负载：{len(questions)} 个真实评测问题 → 展开成真实使用形态 ===")
        workload = build_workload(questions)
        log(f"    总检索次数：{len(workload)}"
            f"（每个问题 × 4 个 Agent 范围 + 原样重问 + 换说法重问）\n")

        # ---- 实测：把同一份负载跑两遍，看**当前实现**的真实命中率
        def run_pass(label: str) -> None:
            reset_cache_stats()
            for item in workload:
                service.search(
                    session,
                    query=item["query"],
                    tenant_id=tenant_id,
                    kb_codes=item["kbs"],
                    max_security_rank=2,
                    top_k=item.get("top_k", 8),
                )
            stats = cache_stats()
            layers = " ".join(
                f"{name}={bucket['hit_rate']:.0%}({bucket['hits']}/{bucket['total_lookups']})"
                for name, bucket in sorted(stats["namespaces"].items())
            )
            log(f"  [{label}] 合计 {stats['hit_rate']:.0%}"
                f"（{stats['hits']}/{stats['total_lookups']}） | {layers}")

        log("=== 实测：同一份负载跑两遍（当前实现）===")
        run_pass("第 1 遍（冷启动：只有任务内复用）")
        run_pass("第 2 遍（同样的问题再问一遍）")
        log("")

        # ---- 对照：三套"键设计"方案的理论上限（用来决定要不要做近似层）
        records = []
        for item in workload:
            hits = service.search(
                session,
                query=item["query"],
                tenant_id=tenant_id,
                kb_codes=item["kbs"],
                max_security_rank=2,
                top_k=8,
            )
            rewritten = service.rewrite_query(item["query"])
            records.append(
                {
                    **item,
                    "rewritten": rewritten,
                    "vector": embedder.embed_query(rewritten),
                    "scope": tuple(sorted(item["kbs"])),
                    "candidates": tuple(sorted(hit.chunk_id for hit in hits)),
                    "top": tuple(hit.chunk_id for hit in hits[:3]),
                }
            )

        # ---- 方案 1：现状（原问题 + 范围 精确）
        def hits_exact() -> int:
            seen = set()
            hits = 0
            for record in records:
                key = (record["query"], record["scope"])
                if key in seen:
                    hits += 1
                seen.add(key)
            return hits

        # ---- 方案 2：改写后精确（rewritten + 范围 精确；无近似）
        def hits_rewritten() -> int:
            seen = set()
            hits = 0
            for record in records:
                key = (record["rewritten"], record["scope"])
                if key in seen:
                    hits += 1
                seen.add(key)
            return hits

        # ---- 方案 3：近似（范围相同 + 向量相似度 ≥ 阈值）
        def evaluate_semantic(threshold: float) -> dict:
            store: dict[tuple, list[dict]] = defaultdict(list)
            hits = 0
            identical_candidates = 0
            top3_same = 0
            sims: list[float] = []
            for record in records:
                bucket = store[record["scope"]]
                best = None
                best_sim = 0.0
                for previous in bucket:
                    sim = cosine_similarity(record["vector"], previous["vector"])
                    if sim > best_sim:
                        best_sim, best = sim, previous
                if best is not None and best_sim >= threshold:
                    hits += 1
                    sims.append(best_sim)
                    if best["candidates"] == record["candidates"]:
                        identical_candidates += 1
                    if best["top"] == record["top"]:
                        top3_same += 1
                bucket.append(record)
            return {
                "hits": hits,
                "identical": identical_candidates,
                "top3_same": top3_same,
                "avg_sim": (sum(sims) / len(sims)) if sims else 0.0,
            }

        total = len(records)
        log("=== 三套方案的命中率 ===")
        log(f"  1) 现状：原问题 + 范围 精确            {hits_exact()}/{total}"
            f" = {hits_exact() / total:.0%}")
        log(f"  2) 改写后精确（无近似，可直接上）        {hits_rewritten()}/{total}"
            f" = {hits_rewritten() / total:.0%}")
        log("  3) 近似（同范围 + 向量相似度阈值）：")
        log(f"      {'阈值':<8}{'命中':<12}{'命中率':<10}{'候选集完全一致':<18}{'Top3 一致':<14}{'平均相似度'}")
        for threshold in (0.90, 0.95, 0.98, 0.99, 0.995, 0.999):
            result = evaluate_semantic(threshold)
            consistency = (result["identical"] / result["hits"]) if result["hits"] else 0.0
            top3 = (result["top3_same"] / result["hits"]) if result["hits"] else 0.0
            log(f"      {threshold:<8}{result['hits']:<12}{result['hits'] / total:<10.0%}"
                f"{consistency:<18.0%}{top3:<14.0%}{result['avg_sim']:.4f}")

        log("\n=== 结论怎么读 ===")
        log("  · 方案 2 是**精确**复用（改写后文本相同 → 向量必然相同 → 候选集必然相同），可以默认开；")
        log("  · 方案 3 是**近似**：复用别人那次查询的候选集，「候选集完全一致」这一列才是它真正的安全边界；")
        log("  · 阈值越高越安全但捞回越少 —— 要靠数据选，不能拍脑袋。")

        # 同一改写文本跨范围（= 上一轮发现的"6 次重复向量化"的根源）统计
        by_rewritten: dict[str, set] = defaultdict(set)
        for record in records:
            by_rewritten[record["rewritten"]].add(record["scope"])
        multi = {text: scopes for text, scopes in by_rewritten.items() if len(scopes) > 1}
        log(f"\n=== 附：同一段改写文本被多个范围使用的情况 ===")
        log(f"  改写文本共 {len(by_rewritten)} 段，其中 {len(multi)} 段被 ≥2 个范围使用"
            f"（这些是方案 2 也救不了的：范围不同，扫的切片本来就不同）")

    out = BACKEND / ".tmp" / "semantic_cache_value.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
