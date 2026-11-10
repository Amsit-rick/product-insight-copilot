"""coverage 口径规则扫描：用采集到的**完整命中词表**离线评估各种改进方案。

## 数据来源

`scripts/calibrate_answerability.py --json ...` 产出的采集结果里，每条用例带有
`all_terms`（问题全部二元组）与 `matched_terms`（在证据文本里命中的那部分）——
这是**权威数据**：它就是工作流实际用的证据上算出来的，不需要重新检索。

（踩过的坑：先前用"脚本自己检索一遍"来预览，结果自取证据与实跑证据中位偏差 0.056、
最大 0.250，足以淹没要观察的差异。所以这里只用实跑记录。）

## 要回答的问题

诊断结论是"漏拒 4 条来自命中泛词/同字异义，词面口径已到上限"。但那 4 条与"1 条被误拒的
可回答用例（客服工单归类，未命中词几乎全是『帮我/归一/一下/几类』这类口语指令碎片）"
病因相反：**前者的分母/分子都是内容词，后者的分母被大量永远匹配不上的碎片撑大**。

于是有两条标准做法可能真的把两类分开（都能用现有数据离线验证）：

1. **去停用词**：丢掉含功能字的二元组（跨词边界碎片）；
2. **IDF 加权**：命中泛词（DF 高）贡献小，命中冷门词贡献大。

IDF 加权有个必须显式处理的点：DF=0 的二元组既可能是"概念缺失"（该罚），也可能是
"碎片"（不该罚），无法区分。所以把 **DF=0 的权重设为可扫描的旋钮**，让数据说话。

## 判据

- `分离度 = min(应当可回答) - max(应当拒答)`，>0 表示存在阈值能完全分开；
- **最优单阈值判定准确率**（24 条建模用例：18 可回答 + 6 应拒答）；
- 相对**生产口径**（`v_both_stop` = 丢掉"全由功能字组成"的二元组）的净收益。

内建校验：`v_both_stop` 的复算值必须与实跑记录的 coverage **完全相等**（偏差 0）。
若不相等，说明脚本里的规则与生产代码已不一致，所有离线结论作废。

## 诚实边界

只有 24 条用例参与判据，**存在过拟合风险**。因此脚本只把"明显更好且机理清楚"的方案列为推荐，
并且任何被选中的方案都必须用真实评测复核（脚本会打印命令）。
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.db.session import SessionLocal  # noqa: E402
from app.models.knowledge import DocumentChunk  # noqa: E402
from app.rag.retrieval import tokenize  # noqa: E402
# 字表直接取自生产代码：扫描的必须是**实际生效**的规则，否则"离线验证通过"毫无意义
from app.services.answerability import FUNCTION_CHARS as STOP_CHARS_WIDE  # noqa: E402

DEFAULT_INPUT = BACKEND / ".tmp" / "calibration_terms.json"

REFUSAL_CATEGORY = "no_answer"
ANSWERABLE_CATEGORIES = ("single_kb", "cross_kb")

# 生产代码当前实际生效的口径（`app/services/answerability.py`）：丢掉"全由功能字组成"的二元组。
# 本脚本必须能精确复现它 —— 否则"离线验证通过"没有意义（内建校验会检查）。
PRODUCTION_RULE = "v_both_stop"

# 精简集：只保留"几乎不可能构成实词"的字（不含 上下里中多少一二两几 —— 它们能构成
# 下降/上线/中位/多次 这类真词）。用来做**敏感性检验**：如果宽集有效而精简集无效，
# 说明结论依赖手工挑选的字表（有过拟合嫌疑），不能采纳。
STOP_CHARS_LEAN = set(
    "的了是和与在有对把被给就都而及或这那哪么什怎呢吗吧啊"
    "我你他她它们咱"
    "会能要可需应该想让请帮"
    "很更太挺"
    "个些次种条份点"
)


def build_df(chunks: list[str]) -> dict[str, int]:
    df: dict[str, int] = {}
    for term in {t for content in chunks for t in set(tokenize(content)) if len(t) >= 2}:
        df[term] = sum(1 for content in chunks if term in content.lower())
    return df


def weight_equal(term: str, df: dict[str, int], total: int, *, unseen: float) -> float:
    return 1.0


def weight_idf(term: str, df: dict[str, int], total: int, *, unseen: float) -> float:
    """平滑 IDF；DF=0 的词按 `unseen` 给权重（可扫描：到底该重罚还是当碎片）。"""
    df_value = df.get(term, 0)
    if df_value == 0:
        return unseen
    return math.log((total + 1) / (df_value + 1)) + 1.0


def keep_term(term: str, mode: str) -> bool:
    if mode == "none":
        return True
    if mode == "any_stop_wide":
        return not (set(term) & STOP_CHARS_WIDE)
    if mode == "any_stop_lean":
        return not (set(term) & STOP_CHARS_LEAN)
    if mode == "both_stop_wide":
        return not set(term) <= STOP_CHARS_WIDE
    raise ValueError(mode)


def coverage(
    row: dict, df: dict[str, int], total: int, *, weighting: str, filter_mode: str, unseen: float
) -> float:
    weight = weight_idf if weighting == "idf" else weight_equal
    terms = [t for t in row["all_terms"] if keep_term(t, filter_mode)]
    matched = set(row["matched_terms"])
    if not terms:
        return 0.0
    denominator = sum(weight(t, df, total, unseen=unseen) for t in terms)
    if denominator <= 0:
        return 0.0
    numerator = sum(weight(t, df, total, unseen=unseen) for t in terms if t in matched)
    return numerator / denominator


def evaluate(values: dict[str, float], rows: list[dict], threshold: float) -> dict:
    answerable = [values[r["case_id"]] for r in rows if r["category"] in ANSWERABLE_CATEGORIES]
    refusal = [values[r["case_id"]] for r in rows if r["category"] == REFUSAL_CATEGORY]
    missed = sum(1 for v in refusal if v >= threshold)
    false_refusal = sum(1 for v in answerable if v < threshold)
    accuracy = (len(answerable) + len(refusal) - missed - false_refusal) / (len(answerable) + len(refusal))
    return {
        "threshold": threshold,
        "missed_refusal": missed,
        "false_refusal": false_refusal,
        "accuracy": accuracy,
        "separation": (min(answerable) - max(refusal)) if answerable and refusal else 0.0,
        "answerable_min": min(answerable) if answerable else 0.0,
        "refusal_max": max(refusal) if refusal else 0.0,
    }


def best_threshold(values: dict[str, float], rows: list[dict]) -> dict:
    """在候选阈值里挑判定准确率最高的；并列时取"更少误拒"的那个。"""
    scores = sorted({values[r["case_id"]] for r in rows})
    candidates = [0.0] + [(a + b) / 2 for a, b in zip(scores, scores[1:])] + [scores[-1] + 0.01]
    results = [evaluate(values, rows, threshold) for threshold in candidates]
    return max(results, key=lambda item: (item["accuracy"], -item["false_refusal"]))


def main() -> int:
    payload_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_INPUT
    if not payload_path.exists():
        print(f"未找到采集结果 {payload_path}，请先运行：")
        print("  python scripts/calibrate_answerability.py --json .tmp/calibration_terms.json")
        return 2
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    rows = []
    for row in payload.get("records", []):
        record = row.get("record")
        if not record or not record.get("all_terms"):
            continue
        rows.append(
            {
                "case_id": row["case_id"],
                "category": row["category"],
                "question": row["question"],
                "passed": row["passed"],
                "all_terms": record["all_terms"],
                "matched_terms": record["matched_terms"],
                "coverage": record["coverage"],
                "top_score": record["top_score"],
            }
        )
    if not rows:
        print("采集结果里没有可用的词表数据（需要带 all_terms 的版本）")
        return 2

    session = SessionLocal()
    try:
        chunks = [c.content or "" for c in session.query(DocumentChunk).all()]
    finally:
        session.close()
    df = build_df(chunks)
    total = len(chunks)

    print(f"=== 数据 ===")
    print(f"  采集结果={payload_path.name} 可分析用例={len(rows)} 语料切片={total}")

    # 内建校验：**生产实际使用的规则**必须能精确复现实跑记录的 coverage，否则说明
    # 扫描的规则与线上不一致（离线结论一律作废）。
    production = {
        r["case_id"]: coverage(r, df, total, weighting="equal", filter_mode="both_stop_wide", unseen=1.0)
        for r in rows
    }
    deltas = sorted(abs(production[r["case_id"]] - r["coverage"]) for r in rows)
    print(
        f"  校验（生产规则 {PRODUCTION_RULE}）：复算 coverage 与实跑记录偏差 中位={deltas[len(deltas) // 2]:.4f} "
        f"最大={deltas[-1]:.4f}（应为 0）"
    )

    variants: list[tuple[str, str, str, float]] = [
        ("v_equal", "equal", "none", 1.0),
        (PRODUCTION_RULE, "equal", "both_stop_wide", 1.0),
        ("v_stop_lean", "equal", "any_stop_lean", 1.0),
        ("v_stop_wide", "equal", "any_stop_wide", 1.0),
        ("v_idf_u1", "idf", "none", 1.0),
        ("v_idf_u2", "idf", "none", 2.0),
        ("v_stop_wide_idf_u2", "idf", "any_stop_wide", 2.0),
        ("v_stop_wide_idf_u1", "idf", "any_stop_wide", 1.0),
    ]
    print("\n=== 方案对比（24 条建模用例：18 应当可回答 / 6 应当拒答）===")
    print(
        f"{'方案':<16}{'分离度':>9}{'可答min':>9}{'拒答max':>9}"
        f"{'最优阈值':>9}{'准确率':>8}{'误拒':>6}{'漏拒':>6}  较生产最优"
    )
    # 基准必须是"生产口径的最优点"，而不是"生产口径沿用某个旧阈值" —— 否则每个方案都显得在进步，
    # 那是把"重标定阈值"的收益算到了"改口径"头上。
    baseline = best_threshold(production, rows)
    table: list[dict] = []
    for name, weighting, filter_mode, unseen in variants:
        values = {
            r["case_id"]: coverage(
                r, df, total, weighting=weighting, filter_mode=filter_mode, unseen=unseen
            )
            for r in rows
        }
        best = best_threshold(values, rows)
        best["name"] = name
        best["values"] = values
        # 沿用现阈值时的表现（口径换了本该重标定，所以"最优阈值"与"沿用 0.2"两栏都给）
        at_default = evaluate(values, rows, 0.2)
        delta = (best["accuracy"] - baseline["accuracy"]) * 24
        tag = "（生产口径）" if name == PRODUCTION_RULE else (
            f"{delta:+.0f} 条（沿用 0.2 时 误拒{at_default['false_refusal']}/漏拒{at_default['missed_refusal']}）"
        )
        print(
            f"{name:<16}{best['separation']:>+9.3f}{best['answerable_min']:>9.3f}"
            f"{best['refusal_max']:>9.3f}{best['threshold']:>9.3f}{best['accuracy']:>8.3f}"
            f"{best['false_refusal']:>6}{best['missed_refusal']:>6}  {tag}"
        )
        table.append(best)
    best_row = max(table, key=lambda item: (item["accuracy"], -item["false_refusal"]))
    print(
        f"\n  生产口径（{PRODUCTION_RULE}）最优点：误拒={baseline['false_refusal']} "
        f"漏拒={baseline['missed_refusal']} 准确率={baseline['accuracy']:.3f} "
        f"（阈值 {baseline['threshold']:.3f}；实跑用的是通过率最优的 0.32，两者判定准确率相同）"
    )
    print(f"  最优方案 = {best_row['name']}（准确率 {best_row['accuracy']:.3f}，阈值 {best_row['threshold']:.3f}）")

    print("\n=== 关注用例在各方案下的取值 ===")
    interesting = [
        r
        for r in rows
        if r["category"] == REFUSAL_CATEGORY
        or (r["category"] in ANSWERABLE_CATEGORIES and r["coverage"] < 0.25)
    ]
    names = [v[0] for v in variants]
    print(f"{'类别':<12}" + "".join(f"{n:>15}" for n in names) + "  问题")
    for row in sorted(interesting, key=lambda r: r["category"]):
        line = f"{row['category']:<12}"
        for name in names:
            variant = next(v for v in table if v["name"] == name)
            line += f"{variant['values'][row['case_id']]:>15.3f}"
        line += f"  {row['question'][:34]}"
        print(line)

    print("\n=== 结论 ===")
    if best_row["separation"] > 0:
        print("  存在能**完全分开**两类用例的口径：换用它即可同时做到零误拒与零漏拒。")
    else:
        print(
            "  所有口径下两类仍然重叠 —— 词面度量存在结构上限（存在「应当拒答」用例在特征上"
            "高于「应当可回答」用例的情形），需要语义信号（真实模型复核 / 语义 embedding）才能跨过。"
        )
    print("  任何口径改动都必须真实复核：")
    print("    python scripts/calibrate_answerability.py --json .tmp/calibration_terms.json")
    print(
        "  另外注意判据不同：本脚本按 24 条建模用例的**判定准确率**选点，"
        "标定脚本按**全量 50 条的通过数**选点；要落地阈值请用后者（并通过 --thresholds 实测复核）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
