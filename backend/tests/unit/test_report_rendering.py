"""报告渲染（`app.services.report_service`）单元测试。

`render_markdown` 是导出与人工审核共用的呈现层，因此这里验证的是
"报告里有什么、Markdown 里就必须能看到什么"：四类陈述、冲突不合并、
引用编号与证据清单一一对应、低置信度必须显式提示。
"""

from __future__ import annotations

import pytest

from app.graph.nodes.evidence import PRIORITY_RULE_TEXT
from app.services.report_service import citation_marks, render_markdown

EVIDENCE = [
    {
        "evidence_id": "E1",
        "source_kind": "document",
        "source_name": "产品需求文档 v3.md",
        "page": 2,
        "document_version": "v3.2.0",
        "content": "3.2.0 版本改动了登录与启动链路。",
    },
    {
        "evidence_id": "E2",
        "source_kind": "metric",
        "source_name": "崩溃监控",
        "metric_name": "crash_rate",
        "dataset_version": "ds-20240301",
        "content": "崩溃率环比上升 12%。",
    },
]


def _report(**overrides) -> dict:
    report = {
        "summary": "3.2.0 上线后崩溃率上升，怀疑与启动链路改动相关。",
        "priority": "P0",
        "confidence": 0.82,
        "risk_level": "medium",
        "approval_status": "pending",
        "impact_scope": [{"dimension": "平台", "value": "Android", "evidence_refs": ["E2"]}],
        "facts": [
            {
                "id": "F1",
                "text": "崩溃率环比上升 12%",
                "evidence_refs": ["E1", "E2"],
                "basis": "崩溃监控 03-01 至 03-05 日数据",
            }
        ],
        "inferences": [
            {"id": "I1", "text": "启动链路改动可能是主因", "evidence_refs": ["E1"], "confidence": 0.7}
        ],
        "hypotheses": [
            {"id": "H1", "text": "Android 12 机型受影响更明显", "verify_by": "分机型灰度对比"}
        ],
        "recommendations": [
            {
                "id": "R1",
                "text": "先回滚 3.2.0 的启动优化改动",
                "evidence_refs": ["E2"],
                "priority": "P0",
                "owner_role": "研发负责人",
            }
        ],
        "conflicts": [
            {
                "id": "conflict-1",
                "topic": "崩溃与闪退",
                "statement_a": {"id": "F1", "text": "崩溃率上升 12%", "agent_code": "product_agent"},
                "statement_b": {
                    "id": "F2",
                    "text": "未发现崩溃异常",
                    "agent_code": "release_agent",
                },
                "resolution": {
                    "adopted_statement": "F1",
                    "adopted_source": "product_agent",
                    "priority_rule": PRIORITY_RULE_TEXT,
                    "reason": "指标口径优先于发布说明",
                },
            }
        ],
        "warnings": ["指标数据只覆盖到 03-05"],
        "data_gaps": ["缺少 iOS 端崩溃数据"],
        "evidence": EVIDENCE,
    }
    report.update(overrides)
    return report


# --------------------------------------------------------------------------- 引用编号
def test_引用编号按证据顺序生成():
    marks = citation_marks([], EVIDENCE)

    assert list(marks) == ["E1", "E2"]
    assert marks["E1"] == "[1] 产品需求文档 v3.md p2 (v3.2.0)"
    assert marks["E2"] == "[2] 崩溃监控 [ds-20240301]"


def test_缺少证据ID时用位置编号():
    marks = citation_marks([], [{"source_name": "甲"}, {"source_name": "乙"}])

    assert list(marks) == ["E1", "E2"]
    assert marks["E2"].startswith("[2] 乙")


def test_指标证据没有来源名时降级为指标名():
    marks = citation_marks([], [{"metric_name": "crash_rate", "source_kind": "metric"}])

    assert marks["E1"] == "[1] 指标:crash_rate"


def test_既无来源名也无指标名时标为未知来源():
    marks = citation_marks([], [{"content": "一段没有出处的文本"}])

    assert marks["E1"] == "[1] 未知来源"


def test_文档证据不带数据版本后缀():
    marks = citation_marks(
        [], [{"source_name": "周报.md", "document_version": "v1", "dataset_version": "ds-1"}]
    )

    assert marks["E1"] == "[1] 周报.md (v1)"


# --------------------------------------------------------------------------- 全量渲染
def test_渲染出全部章节():
    md = render_markdown(_report(), report_id="rep-1", version=2)

    for heading in (
        "# 产品问题诊断报告",
        "## 结论摘要",
        "## 影响范围",
        "## 事实",
        "## 推断",
        "## 假设",
        "## 建议",
        "## 证据冲突（未强行合并）",
        "## 不确定性与数据缺口",
        "## 证据清单",
    ):
        assert heading in md

    assert "- 报告 ID：`rep-1`（v2）" in md
    assert "- 优先级：P0" in md
    assert "- 置信度：0.82" in md
    assert "- 风险等级：medium" in md
    assert "- 审核状态：pending" in md
    assert "3.2.0 上线后崩溃率上升，怀疑与启动链路改动相关。" in md


def test_四类陈述各归入自己的章节():
    md = render_markdown(_report())

    assert "- **F1** 崩溃率环比上升 12% [1] [2]" in md
    assert "  - 依据：崩溃监控 03-01 至 03-05 日数据" in md
    assert "- **I1** 启动链路改动可能是主因 [1]" in md
    assert "- **H1** Android 12 机型受影响更明显" in md
    assert "  - 验证方式：分机型灰度对比" in md
    assert "- **R1** 先回滚 3.2.0 的启动优化改动 [2]" in md
    assert "  - 优先级：P0｜责任角色：研发负责人" in md


def test_引用标记按证据顺序映射到编号():
    md = render_markdown(_report())

    # F1 同时引用 E1、E2，编号必须与证据清单一致且保序
    assert "[1] [2]" in md
    assert "- [1] 产品需求文档 v3.md p2 (v3.2.0) — `E1`" in md
    assert "- [2] 崩溃监控 [ds-20240301] — `E2`" in md
    # 未在证据清单里的引用不会凭空造出编号
    assert "[3]" not in md


def test_引用不存在的证据时不给编号():
    report = _report(facts=[{"id": "F1", "text": "无出处结论", "evidence_refs": ["E9"]}])

    md = render_markdown(report)

    assert "- **F1** 无出处结论\n" in md
    assert "[1]" not in md.split("## 事实")[1].split("##")[0]


def test_影响范围表格带证据编号():
    md = render_markdown(_report())

    assert "| 维度 | 取值 | 证据 |" in md
    assert "| 平台 | Android | [2] |" in md


def test_无引用时影响范围证据列用横线占位():
    md = render_markdown(_report(impact_scope=[{"dimension": "平台", "value": "iOS"}]))

    assert "| 平台 | iOS | - |" in md


# --------------------------------------------------------------------------- 冲突
def test_冲突展示双方结论与采用的来源优先级():
    md = render_markdown(_report())

    assert "### 崩溃与闪退" in md
    assert "- 来源 A（product_agent）：崩溃率上升 12%" in md
    assert "- 来源 B（release_agent）：未发现崩溃异常" in md
    assert "- 采用：`F1`（product_agent）；原因：指标口径优先于发布说明" in md
    assert f"- 来源优先级规则：{PRIORITY_RULE_TEXT}" in md


def test_未裁决的冲突标为待人工裁决():
    report = _report(
        conflicts=[
            {
                "topic": "留存",
                "statement_a": {"id": "F1", "text": "留存下降", "agent_code": "product_agent"},
                "statement_b": {"id": "F2", "text": "留存平稳", "agent_code": "release_agent"},
            }
        ]
    )

    md = render_markdown(report)

    assert "### 留存" in md
    assert "- 采用：待人工裁决" in md
    assert "来源优先级规则" not in md


def test_没有冲突时不渲染冲突章节():
    md = render_markdown(_report(conflicts=[]))

    assert "证据冲突" not in md


# --------------------------------------------------------------------------- 缺口与置信度
def test_不确定性与数据缺口章节():
    md = render_markdown(_report())

    assert "- 数据缺口：缺少 iOS 端崩溃数据" in md
    assert "- 提示：指标数据只覆盖到 03-05" in md


def test_低置信度报告给出复核提示():
    md = render_markdown(_report(confidence=0.45))

    assert "- 置信度：0.45" in md
    assert "> 提示：本次诊断置信度偏低，建议补充材料或由人工复核后再做决策。" in md


def test_置信度达标时不给提示():
    md = render_markdown(_report(confidence=0.6))

    assert "置信度偏低" not in md


def test_未评估置信度时显示未评估且不给提示():
    md = render_markdown(_report(confidence=None))

    assert "- 置信度：未评估" in md
    assert "置信度偏低" not in md


def test_空报告也能渲染出骨架():
    md = render_markdown({})

    assert md.startswith("# 产品问题诊断报告")
    assert "（无）" in md  # 结论摘要为空时的占位
    assert "- 优先级：未定" in md
    assert "- 风险等级：low" in md


def test_证据清单按传入顺序编号():
    """证据顺序即引用编号顺序，两者不能错位。"""
    reversed_evidence = list(reversed(EVIDENCE))
    report = _report(
        evidence=reversed_evidence,
        facts=[{"id": "F1", "text": "崩溃率上升", "evidence_refs": ["E1"]}],
    )

    md = render_markdown(report)

    assert "- [1] 崩溃监控 [ds-20240301] — `E2`" in md
    assert "- [2] 产品需求文档 v3.md p2 (v3.2.0) — `E1`" in md
    # E1 此时是第 2 条证据，正文里的编号必须跟着变
    assert "- **F1** 崩溃率上升 [2]" in md


@pytest.mark.parametrize("confidence", [0.0, 0.45, 0.59])
def test_低置信度阈值边界(confidence):
    assert "置信度偏低" in render_markdown(_report(confidence=confidence))
