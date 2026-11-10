"""可回答性阈值标定（阶段B：检索质量提升 · 离线可量化）。

## 为什么需要这个脚本

`answerability_min_coverage`（默认 0.2）与 `answerability_high_score`（默认 0.26）这两个阈值
是**拍出来的**，但它们直接决定报告里到底有没有事实结论：

- 判"不可回答" → `report.py` 把 facts/inferences 全部降级为假设、摘要改写为"证据不足…"
  → `no_answer` 用例因此**应当**通过（这正是我们要的拒答）；
- 判"不可回答" 也会让正常的 `single_kb` / `cross_kb` 用例**丢光事实** → `has_facts=False` → 用例失败。

也就是说，这两个数字在"该拒的拒掉"和"别误拒"之间划了一条线，而线画在哪里一直没有数据支撑
（README §8 已记录：离线哈希向量下覆盖率天然偏低，阈值需要重新标定）。

## 做法（为什么不用为每个阈值重跑一遍评测）

`answerability_check` 在 stub 模型下不参与（`_model_answerability` 见到 `usage.stub` 直接返回 None），
所以判定 **100% 由词面规则决定**：`answerable = coverage >= c or top_score >= h`。
于是只要采集到每条用例的 `(coverage, top_score)`，就能**离线精确复算**任意阈值下的判定结果，
再用"判定 → 通过与否"的确定性映射（见 `predict_pass`）算出预测通过数。

一次完整评测（50 条）只跑一遍，网格（13×11=143 个点）在内存里算完。

## 诚实边界

1. 临时库（`backend/.tmp/calib_answerability.db`）播种后跑，**绝不碰开发库**；
2. 采集钩子挂在 `nodes/evidence.py::_build_coverage` 上：它返回的 `answerability` 就是最终判定；
   如果出现 `decided_by=model`（配了真实模型），脚本会**拒绝给推荐**——那种情况下判定不由阈值单独决定；
3. 通过数推演只覆盖 `no_answer` / `single_kb` / `cross_kb` 三类（它们的通过判定确实只依赖可回答性位）；
   其他类别（conflict_evidence / prompt_injection / data_missing / version_time_mismatch）不建模，
   按原结果计入并单独列出——改阈值可能连带影响它们的 `needs_human_review`，必须靠真实复跑确认；
4. 所以 `--apply` 之后**必须**重跑完整评测复核（脚本会把命令打出来）。

用法::

    python scripts/calibrate_answerability.py                  # 采集 + 扫网格 + 出推荐
    python scripts/calibrate_answerability.py --limit 12       # 快速试跑
    python scripts/calibrate_answerability.py --apply          # 把推荐阈值写进 backend/.env
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import sys
import threading
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

TMP_DIR = BACKEND / ".tmp"
CALIB_DB = TMP_DIR / "calib_answerability.db"

# 采集期观察到的记录：每调用一次 _build_coverage 追加一条
_RECORDS: list[dict] = []
_LOCK = threading.Lock()

# 判定只依赖可回答性位的类别；其余类别不参与推演
MODELED_REFUSAL = "no_answer"
MODELED_ANSWERABLE = ("single_kb", "cross_kb")
UNMODELED = ("conflict_evidence", "prompt_injection", "data_missing", "version_time_mismatch")
NEVER_REACHES = ("unauthorized",)  # 权限层就失败了，走不到证据节点


# --------------------------------------------------------------------------- 环境
def prepare_env(db_path: Path, *, thresholds: str | None = None) -> None:
    """在 import app.* 之前把环境钉死（`get_settings` 是 lru_cache，之后改环境无效）。

    钉死的四项都有理由：
    - `DATABASE_URL`：临时库，不碰开发库；
    - `LLM_PROVIDER=stub`：本脚本要的是**离线**判定口径，且真实模型会花钱；
    - `CACHE_BACKEND=memory`：避免开发库 Redis 里的旧缓存串味（换库后 key 仍可能撞上）；
    - `VECTOR_BACKEND=fallback`：向量随切片存在临时库里，不依赖 Qdrant/pgvector 在线。

    `thresholds="0.25,0.26"` 时用指定阈值跑（用于**实测验证**离线网格给出的预测——
    网格是离线复算，必须用真实评测确认，否则只是纸上推演）。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    os.environ["LLM_PROVIDER"] = "stub"
    os.environ["CACHE_BACKEND"] = "memory"
    os.environ["VECTOR_BACKEND"] = "fallback"
    os.environ["RERANKER_ENDPOINT"] = ""
    if thresholds:
        coverage, _, high = thresholds.partition(",")
        os.environ["ANSWERABILITY_MIN_COVERAGE"] = coverage.strip()
        os.environ["ANSWERABILITY_HIGH_SCORE"] = (high or coverage).strip()


# --------------------------------------------------------------------------- 采集
def install_recorder() -> None:
    """把记录钩子挂到 `_build_coverage` 上（局部 import 保证补丁作用于真实模块对象）。"""
    from app.graph.nodes import evidence as evidence_node

    original = evidence_node._build_coverage

    @functools.wraps(original)
    def wrapper(state, evidence, statements, conflicts, settings):
        result = original(state, evidence, statements, conflicts, settings)
        try:
            _record(state, evidence, result)
        except Exception:  # 记录失败绝不能影响评测本身
            pass
        return result

    evidence_node._build_coverage = wrapper


def _record(state: dict, evidence: list[dict], result: dict) -> None:
    """记录判定输入。除覆盖率外还记**完整的命中词表**——它是下一步改进度量的唯一依据。

    为什么要记完整词表：`Coverage.missing_terms` 被截断到 12 个，而"哪些词命中/没命中"
    正是判断改进口径（去停用词 / IDF 加权）能否奏效的原始数据。重算词表的依据必须与
    `_answerability` 完全一致：全部证据的 content 拼起来（指标类证据也参与文本覆盖率），
    只有 top_score 排除指标类证据。
    """
    from app.rag.retrieval import tokenize

    answerability = (result or {}).get("answerability") or {}
    query = state.get("query") or ""
    terms = sorted({t for t in tokenize(query) if len(t) >= 2})
    blob = " ".join((item.get("content") or "").lower() for item in evidence)
    matched = [t for t in terms if t in blob]
    row = {
        "task_id": state.get("task_id"),
        "query": query,
        "coverage": float(answerability.get("coverage") or 0.0),
        "top_score": float(answerability.get("top_score") or 0.0),
        "terms": answerability.get("terms"),
        "matched": answerability.get("matched"),
        "all_terms": terms,
        "matched_terms": matched,
        "missing_terms": answerability.get("missing_terms") or [],
        "answerable": bool(answerability.get("answerable")),
        "decided_by": answerability.get("decided_by") or "rule",
        "threshold": answerability.get("threshold"),
        "high_score_escape": answerability.get("high_score_escape"),
        "evidence_count": result.get("evidence_count"),
        "document_evidence_count": result.get("document_evidence_count"),
        "fact_count": result.get("fact_count"),
        "data_gaps": result.get("data_gaps") or [],
    }
    with _LOCK:
        _RECORDS.append(row)


def join_records(cases: list[dict]) -> list[dict]:
    """把采集记录按 task_id 拼到评测用例结果上（同一任务多次调用时取最后一次）。"""
    latest: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for row in _RECORDS:
        task_id = row["task_id"]
        counts[task_id] = counts.get(task_id, 0) + 1
        latest[task_id] = row

    joined: list[dict] = []
    for case in cases:
        record = latest.get(case["task_id"])
        joined.append({**case, "record": record, "record_calls": counts.get(case["task_id"], 0)})
    return joined


# --------------------------------------------------------------------------- 判定模型
def judge(record: dict, coverage_threshold: float, high_score: float) -> bool:
    """与 `services/answerability.py` 完全同一条公式。"""
    return record["coverage"] >= coverage_threshold or record["top_score"] >= high_score


def predict_pass(row: dict, answerable: bool) -> bool:
    """预测"可回答性位 = answerable"时该用例是否通过。

    依据（`report.py` 与 `evaluation_service.py::_evaluate_case` 的实际代码）：

    - 不可回答 → facts/inferences 被清空，摘要前缀"证据不足，无法回答该问题…"：
      * `no_answer` 的 `refused_or_insufficient` = ("证据不足" in summary) or (not facts) → **通过**；
        唯一可能不通过的是 `completed`（任务本身没跑完），这一项与阈值无关，按实测值带入。
      * `single_kb`/`cross_kb` 的 `has_facts` = bool(facts) → **必然失败**。
    - 可回答 → 反过来：`no_answer` **必然失败**（事实被保留、摘要里没有"证据不足"），
      `single_kb`/`cross_kb` 恢复事实 → **通过**。

    **关键**：`row["passed"]` 是"在**实跑阈值**下"的结果，所以只有当前判定与实跑判定一致时
    才能直接沿用；一旦翻转，必须按上面的确定性规则推演，不能沿用实测值。
    （踩过：把实测值一律当成"可回答时的结果"，网格预测的通过数整体偏高 1 条。）

    其他类别不建模（返回实测值），并在报告里单独提示 `other_flips`。
    """
    category = row["category"]
    observed = bool(row["passed"])
    checks = row.get("checks") or {}
    record = row.get("record")
    if record is not None and answerable == bool(record["answerable"]):
        return observed  # 判定未翻转，直接沿用实跑结果
    if category == MODELED_REFUSAL:
        return bool(checks.get("completed", False)) if not answerable else False
    if category in MODELED_ANSWERABLE:
        return answerable
    return observed


# --------------------------------------------------------------------------- 统计
def build_grid(coverage_values: list[float], high_values: list[float]) -> list[dict]:
    points: list[dict] = []
    for coverage in coverage_values:
        for high in high_values:
            missed_refusal = 0  # 该拒没拒（no_answer 被判可回答）
            correct_refusal = 0
            false_refusal = 0  # 误拒（single_kb/cross_kb 被判不可回答）
            correct_answer = 0
            total_pass = 0
            other_flips = 0
            for row in CURRENT:
                if row["record"] is None:
                    # 权限层就被拒的用例（unauthorized）：判定与阈值无关，按实测计入通过数
                    total_pass += int(bool(row["passed"]))
                    continue
                answerable = judge(row["record"], coverage, high)
                predicted = predict_pass(row, answerable)
                total_pass += int(predicted)
                category = row["category"]
                if category == MODELED_REFUSAL:
                    if answerable:
                        missed_refusal += 1
                    else:
                        correct_refusal += 1
                elif category in MODELED_ANSWERABLE:
                    if answerable:
                        correct_answer += 1
                    else:
                        false_refusal += 1
                elif answerable != row["record"]["answerable"]:
                    other_flips += 1
            points.append(
                {
                    "coverage": round(coverage, 3),
                    "high_score": round(high, 3),
                    "missed_refusal": missed_refusal,
                    "correct_refusal": correct_refusal,
                    "false_refusal": false_refusal,
                    "correct_answer": correct_answer,
                    "total_pass": total_pass,
                    "total_fail": TOTAL_CASES - total_pass,
                    "other_flips": other_flips,
                    "accuracy": (
                        round(
                            (correct_refusal + correct_answer)
                            / (correct_refusal + correct_answer + missed_refusal + false_refusal),
                            4,
                        )
                        if (correct_refusal + correct_answer + missed_refusal + false_refusal)
                        else None
                    ),
                }
            )
    return points


def rank(points: list[dict], *, baseline_false_refusal: int) -> list[dict]:
    """排序口径：**总失败数**（误拒 + 漏拒）最少优先，并列时更少误拒优先。

    为什么以总失败数为第一目标：评测的首指标就是通过率，两种失败在评测里权重相同（各算一条）。
    但两种失败在**产品语义**上并不等价（误拒＝本该答的问题答不了；漏拒＝用不相关内容当结论），
    而这个权重取决于业务容忍度、无法从数据里推出来 —— 所以同时给出"零误拒"子集，
    让使用者可以按自己的偏好选（`zero_false_refusal`）。
    """
    ordered = sorted(
        points,
        key=lambda p: (
            p["false_refusal"] + p["missed_refusal"],
            p["false_refusal"],
            -p["total_pass"],
            p["coverage"],
        ),
    )
    return ordered or points


def zero_false_refusal(points: list[dict], *, baseline_false_refusal: int) -> list[dict]:
    """"不接受任何误拒"策略下的最优点（按总失败数排序）。"""
    feasible = [p for p in points if p["false_refusal"] <= baseline_false_refusal]
    return rank(feasible or points, baseline_false_refusal=baseline_false_refusal)


# --------------------------------------------------------------------------- 输出
def fmt(value: float | None, digits: int = 4) -> str:
    return "  n/a" if value is None else f"{value:.{digits}f}"


def print_cases(rows: list[dict]) -> None:
    print("\n=== 逐用例：采集到的判定输入与结果 ===")
    print(f"{'类别':<20}{'cov':>7}{'top':>8}{'terms':>7}{'判定':>8}{'用例':>6}  失败判定项")
    for row in sorted(rows, key=lambda r: (r["category"], r["question"])):
        record = row["record"]
        failed = [name for name, ok in (row.get("checks") or {}).items() if not ok]
        if record is None:
            note = "权限层拦截（未进入证据阶段）" if row["category"] in NEVER_REACHES else "未采集"
            print(f"{row['category']:<20}{'--':>7}{'--':>8}{'--':>7}{note:>8}{'--':>6}  {note}")
            continue
        print(
            f"{row['category']:<20}{fmt(record['coverage'], 3):>7}{fmt(record['top_score'], 3):>8}"
            f"{str(record['terms']):>7}{'可答' if record['answerable'] else '不可答':>8}"
            f"{'通过' if row['passed'] else '失败':>6}  {','.join(failed) or '-'}"
        )


def print_separability(rows: list[dict]) -> dict:
    print("\n=== 类别可分性（判定输入能不能分开「该答」与「该拒」）===")
    buckets: dict[str, list[float]] = {}
    tops: dict[str, list[float]] = {}
    for row in rows:
        record = row["record"]
        if record is None:
            continue
        key = MODELED_REFUSAL if row["category"] == MODELED_REFUSAL else (
            "answerable(单/跨库)" if row["category"] in MODELED_ANSWERABLE else "其他"
        )
        buckets.setdefault(key, []).append(record["coverage"])
        tops.setdefault(key, []).append(record["top_score"])
    stats: dict = {}
    for key in ("answerable(单/跨库)", MODELED_REFUSAL, "其他"):
        values = buckets.get(key) or []
        if not values:
            continue
        ordered = sorted(values)
        stats[key] = {
            "n": len(values),
            "min": ordered[0],
            "median": ordered[len(ordered) // 2],
            "max": ordered[-1],
            "top_max": max(tops.get(key) or [0.0]),
        }
        print(
            f"  {key:<20} n={len(values):<3} coverage min={ordered[0]:.3f} "
            f"中位={ordered[len(ordered) // 2]:.3f} max={ordered[-1]:.3f}  "
            f"top_score max={max(tops.get(key) or [0.0]):.3f}"
        )
    answerable = stats.get("answerable(单/跨库)")
    refusal = stats.get(MODELED_REFUSAL)
    if answerable and refusal:
        if refusal["max"] < answerable["min"]:
            print(f"  → 完全可分：coverage 落在 ({refusal['max']:.3f}, {answerable['min']:.3f}) 区间内即可完美区分")
        else:
            print(
                f"  → **存在重叠**：[{answerable['min']:.3f}, {refusal['max']:.3f}] 区间内既有应当可回答的用例、"
                f"也有应当拒答的用例，单靠词面覆盖率不可能完全分开（这是本方案的已知上限）"
            )
    return stats


def print_grid(points: list[dict], ranked: list[dict]) -> None:
    print("\n=== 阈值网格（每个 coverage 取值下选最优 high_score）===")
    print(
        f"{'coverage':>9}{'high':>8}{'漏拒':>6}{'该拒已拒':>9}{'误拒':>6}{'正确可答':>9}"
        f"{'预测通过':>9}{'准确率':>8}"
    )
    by_coverage: dict[float, dict] = {}
    # 每个 coverage 下选"总失败数最少"的 high_score（与推荐口径一致）
    for point in sorted(
        points, key=lambda p: (p["total_fail"], p["false_refusal"], p["missed_refusal"], p["high_score"])
    ):
        by_coverage.setdefault(point["coverage"], point)
    for coverage in sorted(by_coverage):
        point = by_coverage[coverage]
        accuracy = "n/a" if point["accuracy"] is None else f"{point['accuracy']:.3f}"
        print(
            f"{point['coverage']:>9.3f}{point['high_score']:>8.3f}{point['missed_refusal']:>6}"
            f"{point['correct_refusal']:>9}{point['false_refusal']:>6}{point['correct_answer']:>9}"
            f"{point['total_pass']:>9}{accuracy:>8}"
        )
    print(f"\n=== Top 8 工作点（按：总失败数 → 误拒 → 总通过数；共 {TOTAL_CASES} 条用例）===")
    for index, point in enumerate(ranked[:8], start=1):
        accuracy = "n/a" if point["accuracy"] is None else f"{point['accuracy']:.3f}"
        print(
            f"  #{index} coverage>={point['coverage']:.3f} 或 top_score>={point['high_score']:.3f}"
            f"  误拒={point['false_refusal']} 漏拒={point['missed_refusal']}"
            f"  预测通过={point['total_pass']}/{TOTAL_CASES} 准确率={accuracy}"
        )


def print_unfixable(rows: list[dict], current_false_refusal: int) -> None:
    print("\n=== 剩余失败用例（在实跑阈值下判错的那几条）===")
    any_found = False
    for row in rows:
        record = row["record"]
        if record is None or row["passed"]:
            continue
        if row["category"] == MODELED_REFUSAL and not record["answerable"]:
            any_found = True
            print(
                f"  [{row['category']}] 已判「不可回答」却仍失败："
                f"{','.join(k for k, v in (row.get('checks') or {}).items() if not v)}"
                f"  问题：{row['question'][:44]}"
            )
        elif row["category"] in MODELED_ANSWERABLE and record["answerable"]:
            any_found = True
            print(
                f"  [{row['category']}] 已判「可回答」却仍失败："
                f"{','.join(k for k, v in (row.get('checks') or {}).items() if not v)}"
                f"  问题：{row['question'][:44]}"
            )
    if not any_found:
        print("  （无：当前所有失败都可由阈值修正）")
    if current_false_refusal:
        print(f"  其中「误拒」类共 {current_false_refusal} 条：这些单靠调阈值只能靠放宽阈值救回（会同时放大漏拒）")


def upsert_env(path: Path, updates: dict[str, str]) -> list[str]:
    """把键值写进 .env（已存在则替换，不存在则追加），返回变更说明。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    backup = path.with_suffix(path.suffix + ".bak")
    if path.exists():
        backup.write_text("\n".join(lines) + "\n", encoding="utf-8")
    changed: list[str] = []
    seen: set[str] = set()
    for index, line in enumerate(lines):
        stripped = line.strip()
        for key, value in updates.items():
            if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                if stripped != f"{key}={value}":
                    changed.append(f"{line.strip()}  →  {key}={value}")
                    lines[index] = f"{key}={value}"
                seen.add(key)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")
            changed.append(f"（新增）{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return changed


# --------------------------------------------------------------------------- 主流程
CURRENT: list[dict] = []
TOTAL_CASES = 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="可回答性阈值标定")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="只跑前 N 条用例（注意：按类别字母序取，适合验证脚本链路，不适合出结论）",
    )
    parser.add_argument(
        "--coverage-grid",
        default="0.05,0.1,0.15,0.2,0.25,0.28,0.3,0.32,0.35,0.4,0.45,0.5",
    )
    parser.add_argument("--high-grid", default="0.05,0.1,0.15,0.2,0.26,0.3,0.35,0.4,0.5,0.6")
    parser.add_argument("--apply", action="store_true", help="把推荐阈值写进 backend/.env（会先备份）")
    parser.add_argument(
        "--thresholds",
        default=None,
        help="用指定阈值跑本次评测（格式 coverage,high_score），用于实测验证离线网格的预测",
    )
    parser.add_argument("--json", dest="json_path", default=None, help="把完整结果另存为 JSON")
    args = parser.parse_args(argv)

    prepare_env(CALIB_DB, thresholds=args.thresholds)

    from app.config import get_settings
    from app.db.seed import init_db, seed_all
    from app.db.session import SessionLocal
    from app.models.identity import Tenant
    from app.services.evaluation_service import EvaluationService

    settings = get_settings()
    if settings.llm_provider != "stub":
        print(f"!! 需要离线口径（LLM_PROVIDER=stub），当前={settings.llm_provider}")
        return 2

    print("=== 环境 ===")
    print(f"  临时候选库={CALIB_DB}")
    print(f"  模型={settings.llm_provider}  向量后端={settings.vector_backend}  维度={settings.embedding_dim}")
    print(f"  当前阈值：coverage>={settings.answerability_min_coverage} or top_score>={settings.answerability_high_score}")

    print("\n=== 1/4 播种临时库（与离线基线同一份数据）===")
    init_db()
    summary = seed_all()
    print(f"  租户={summary['tenant']} 知识库={summary['knowledge_bases']} "
          f"文档={summary['documents']} 指标={len(summary['metrics'])} 评测用例={summary['eval_cases']}")

    print("\n=== 2/4 采集：跑一次完整评测（记录每条用例的判定输入）===")
    install_recorder()
    session = SessionLocal()
    try:
        tenant = session.query(Tenant).filter(Tenant.code == summary["tenant"]).first()
        service = EvaluationService(session)
        cases = service.load_cases(tenant_id=tenant.id, limit=args.limit)
        print(f"  用例数={len(cases)}（预计耗时约 {len(cases) * 5}s）")
        run = service.run(tenant_id=tenant.id, name="[标定] 可回答性阈值采集", limit=args.limit)
        metrics = run.metrics or {}
        passed_cases = sum(1 for detail in (run.details or []) if detail["passed"])
        print(f"  运行状态={run.status} 通过={passed_cases}/{metrics.get('case_count')} "
              f"pass_rate={metrics.get('pass_rate')} 拒答准确率={metrics.get('refusal_accuracy')} "
              f"检查项通过率={metrics.get('check_pass_rate')} 平均耗时={run.avg_latency_ms}ms")
        print(f"  分类通过率={json.dumps(metrics.get('per_category') or {}, ensure_ascii=False)}")
        case_rows = [
            {
                "case_id": detail["case_id"],
                "category": detail["category"],
                "question": detail["question"],
                "task_id": detail["task_id"],
                "passed": detail["passed"],
                "checks": detail["checks"],
            }
            for detail in (run.details or [])
        ]
    finally:
        session.close()

    global CURRENT, TOTAL_CASES
    CURRENT = join_records(case_rows)
    TOTAL_CASES = len(CURRENT)
    collected = [row for row in CURRENT if row["record"] is not None]
    print(f"  采集到判定输入 {len(collected)}/{len(CURRENT)} 条"
          f"（未采集的是权限层就被拒的用例）")
    multi = [row for row in CURRENT if row["record_calls"] > 1]
    if multi:
        print(f"  注意：{len(multi)} 条用例的证据节点执行了多次，已取最后一次判定")
    decided_by_model = [row for row in collected if row["record"]["decided_by"] != "rule"]
    if decided_by_model:
        print(f"  !! {len(decided_by_model)} 条用例的判定由**模型**裁决（decided_by=model）："
              f"阈值不再是唯一决定因素，本报告的推荐不可直接套用")

    print_cases(CURRENT)
    stats = print_separability(CURRENT)

    print("\n=== 3/4 扫阈值网格（离线复算，同一公式）===")
    coverage_values = [float(v) for v in args.coverage_grid.split(",") if v.strip()]
    high_values = [float(v) for v in args.high_grid.split(",") if v.strip()]
    points = build_grid(coverage_values, high_values)
    current_point = next(
        (
            p
            for p in points
            if abs(p["coverage"] - settings.answerability_min_coverage) < 1e-9
            and abs(p["high_score"] - settings.answerability_high_score) < 1e-9
        ),
        None,
    )
    baseline_false_refusal = current_point["false_refusal"] if current_point else sum(
        1
        for row in collected
        if row["category"] in MODELED_ANSWERABLE and not row["record"]["answerable"]
    )
    ranked = rank(points, baseline_false_refusal=baseline_false_refusal)
    print_grid(points, ranked)
    if current_point:
        print(
            f"\n  当前工作点（coverage>={current_point['coverage']} / high>={current_point['high_score']}）在网格内："
            f"误拒={current_point['false_refusal']} 漏拒={current_point['missed_refusal']} "
            f"预测通过={current_point['total_pass']}/{TOTAL_CASES}"
        )
    else:
        print(f"\n  当前工作点不在网格内；当前误拒={baseline_false_refusal}")

    best = ranked[0]
    strict = zero_false_refusal(points, baseline_false_refusal=baseline_false_refusal)[0]
    print("\n=== 推荐工作点（主口径：总失败数最少 = 通过率最高）===")
    print(f"  ANSWERABILITY_MIN_COVERAGE={best['coverage']}")
    print(f"  ANSWERABILITY_HIGH_SCORE={best['high_score']}")
    print(
        f"  对比当前：误拒 {baseline_false_refusal} → {best['false_refusal']}；"
        f"漏拒 {(current_point or {}).get('missed_refusal', '?')} → {best['missed_refusal']}；"
        f"预测通过 {(current_point or {}).get('total_pass', '?')}/{TOTAL_CASES} → "
        f"{best['total_pass']}/{TOTAL_CASES}"
    )
    if strict["coverage"] != best["coverage"] or strict["high_score"] != best["high_score"]:
        print("\n  备选工作点（策略：不接受任何误拒 —— 宁可漏拒也不误拒）")
        print(f"  ANSWERABILITY_MIN_COVERAGE={strict['coverage']}")
        print(f"  ANSWERABILITY_HIGH_SCORE={strict['high_score']}")
        print(
            f"  误拒={strict['false_refusal']} 漏拒={strict['missed_refusal']} "
            f"预测通过={strict['total_pass']}/{TOTAL_CASES}"
        )
        print("  两种策略的差别是**产品取向**而非数据结论：误拒＝本该能答的问题答不了；"
              "漏拒＝用不相关材料给出结论。业务更怕哪一种，就选对应的工作点。")
    if best["false_refusal"] > 0:
        print("  !! 推荐点仍存在误拒：说明这两类用例在词面指标上就是重叠的，"
              "需要换检索/embedding 模型（见 README §8）")
    if decided_by_model:
        print("  !! 存在模型裁决用例，上述推荐仅供参考——真实模型下请重跑本脚本并在提示中排除模型复核")

    print_unfixable(CURRENT, baseline_false_refusal)

    print("\n=== 4/4 落地与复核 ===")
    if args.apply:
        env_path = BACKEND / ".env"
        changes = upsert_env(
            env_path,
            {
                "ANSWERABILITY_MIN_COVERAGE": f"{best['coverage']}",
                "ANSWERABILITY_HIGH_SCORE": f"{best['high_score']}",
            },
        )
        print(f"  已写入 {env_path}（备份 {env_path.name}.bak）：")
        for change in changes or ["（值未变化）"]:
            print(f"    {change}")
        print("  !! 生效需要重启 API/worker（get_settings 有缓存）")
    else:
        print("  未写入配置（加 --apply 才会写 backend/.env，会先备份）")
    print("  复核命令（阈值改变会实际影响报告内容，必须真实复跑）：")
    print("    cd backend && python scripts/run_evaluation.py --name \"阈值标定后复核\"")

    if args.json_path:
        out = Path(args.json_path)
        out.write_text(
            json.dumps(
                {
                    "settings": {
                        "coverage": settings.answerability_min_coverage,
                        "high_score": settings.answerability_high_score,
                    },
                    "separability": stats,
                    "records": collected,
                    "grid": points,
                    "recommended": best,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n  明细已写入 {out}")

    # SQLite 在 Windows 上只要还有连接持有文件句柄就删不掉：必须先 dispose 引擎。
    # （踩过：脚本跑完所有分析却因为清理临时库抛 PermissionError 而以 exit code 1 结束。）
    try:
        from app.db.session import engine as app_engine

        app_engine.dispose()
    except Exception:
        pass
    if CALIB_DB.exists():
        try:
            CALIB_DB.unlink()
            print(f"  临时库已清理（{CALIB_DB.name}）")
        except PermissionError:
            print(f"  临时库仍被占用，未删除：{CALIB_DB}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
