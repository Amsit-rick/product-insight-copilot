"""跨来源证据冲突检测（`app.graph.nodes.evidence.detect_conflicts`）单元测试。

这一层的设计取向是"宁可漏报，也不要堆出一百条噪音冲突"，因此用例重点验证
几条保守规则：只在同一主题上极性相反时才算冲突、跨 Agent、跨文档、
同类型普通文档之间不判冲突、总量封顶。
"""

from __future__ import annotations

from app.graph.nodes.evidence import MAX_CONFLICTS, _polarity, _topic_polarity, detect_conflicts

CRASH = "崩溃与闪退"


def _doc(eid: str, *, doc_id: str, kb: str, name: str) -> dict:
    """一条文档证据：文档 ID 决定"是否同一来源"，知识库决定"来源类型"。"""
    return {
        "evidence_id": eid,
        "source_kind": "document",
        "document_id": doc_id,
        "knowledge_base": kb,
        "source_name": name,
        "content": f"{name} 的正文片段",
        "relevance_score": 0.8,
    }


def _statement(sid: str, text: str, agent: str, refs: list[str], *, type_: str = "fact") -> dict:
    return {
        "id": sid,
        "type": type_,
        "text": text,
        "source_agent": agent,
        "evidence_refs": refs,
        "confidence": 0.8,
        "tags": [],
        "supplemental": False,
    }


# --------------------------------------------------------------------------- 植入的冲突
def test_一方断言一方否认时判定为冲突():
    """产品库说"崩溃上升"，研发发布记录说"未发现崩溃异常"，属跨来源冲突。"""
    statements = [
        _statement("F1", "崩溃率较上一版本上升 12%，问题集中在 Android 端", "product_agent", ["E1"]),
        _statement("F2", "本版本未发现崩溃异常，各项指标正常", "release_agent", ["E2"]),
    ]
    evidence = [
        _doc("E1", doc_id="doc-product", kb="product_kb", name="产品周报"),
        _doc("E2", doc_id="doc-release", kb="engineering_kb", name="发布记录"),
    ]

    conflicts = detect_conflicts(statements, evidence)

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["id"] == "conflict-1"
    assert conflict["topic"] == CRASH
    assert conflict["statement_a"]["agent_code"] == "product_agent"
    assert conflict["statement_a"]["text"] == statements[0]["text"]
    assert conflict["statement_b"]["agent_code"] == "release_agent"
    assert conflict["evidence_refs"] == ["E1", "E2"]
    assert conflict["sources"] == ["产品周报", "发布记录"]
    assert sorted(conflict["source_kinds"]) == ["engineering_kb", "product_kb"]
    assert conflict["detected_by"] == ["product_agent", "release_agent"]
    # 只检测不裁决：裁决留给 resolve_conflict，且必须人工确认
    assert conflict["resolution"] is None


def test_双方都断言时不算冲突():
    statements = [
        _statement("F1", "崩溃率上升 12%", "product_agent", ["E1"]),
        _statement("F2", "崩溃率上升 8%", "release_agent", ["E2"]),
    ]
    evidence = [
        _doc("E1", doc_id="doc-a", kb="product_kb", name="甲"),
        _doc("E2", doc_id="doc-b", kb="engineering_kb", name="乙"),
    ]

    assert detect_conflicts(statements, evidence) == []


def test_双方都否认时不算冲突():
    statements = [
        _statement("F1", "未发现崩溃异常", "product_agent", ["E1"]),
        _statement("F2", "未见崩溃问题", "release_agent", ["E2"]),
    ]
    evidence = [
        _doc("E1", doc_id="doc-a", kb="product_kb", name="甲"),
        _doc("E2", doc_id="doc-b", kb="engineering_kb", name="乙"),
    ]

    assert detect_conflicts(statements, evidence) == []


# --------------------------------------------------------------------------- 极性窗口
def test_否定词离主题很远时不视为否认该主题():
    """"异常检测未发现显著异常点"里的"未发现"离"留存"很远，不能算否认留存。"""
    text = "留存率维持在 42%，异常检测未发现显著异常点"

    assert _topic_polarity(text, {"留存"})["留存"] != "negation"
    # 整句极性确实是 negation，这正是必须逐主题判定的原因
    assert _polarity(text) == "negation"


def test_否定词紧贴主题词时才算否认():
    """判定窗口只看主题词之前 12 个字符：紧贴即算否认，隔得远就不算。"""
    assert _topic_polarity("未发现留存下降", {"留存"})["留存"] == "negation"
    assert _topic_polarity("没有发现留存异常", {"留存"})["留存"] == "negation"
    # 否定词落在主题词之后时窗口扫不到，退回中性——宁可漏判也不误判
    assert _topic_polarity("留存未受影响", {"留存"})["留存"] == "neutral"


def test_远距离否定不触发冲突():
    """整体看是"否认句"，但被否认的是异常点而不是留存下降，不应判冲突。"""
    statements = [
        _statement("F1", "留存率维持在 42%，异常检测未发现显著异常点", "product_agent", ["E1"]),
        _statement("F2", "留存下降明显，需要重点关注", "release_agent", ["E2"]),
    ]
    evidence = [
        _doc("E1", doc_id="doc-a", kb="product_kb", name="甲"),
        _doc("E2", doc_id="doc-b", kb="engineering_kb", name="乙"),
    ]

    assert detect_conflicts(statements, evidence) == []


def test_逐主题给出极性判定():
    """同一句话里"崩溃"被否认、"登录"被断言，两个主题各判各的。"""
    text = "未发现崩溃异常。排查后确认，登录失败率上升"

    verdict = _topic_polarity(text, {CRASH, "登录与账号"})

    assert verdict == {CRASH: "negation", "登录与账号": "assertion"}


def test_定量变化构成断言():
    verdict = _topic_polarity("GMV -16.4%", {"支付与转化"})

    assert verdict == {"支付与转化": "assertion"}


def test_没有极性词时为neutral():
    assert _topic_polarity("留存 42%", {"留存"}) == {"留存": "neutral"}
    assert _polarity("留存 42%") == "neutral"


# --------------------------------------------------------------------------- 来源约束
def test_同一Agent的两条陈述不算冲突():
    """单 Agent 内部的自相矛盾交给它自己复核，不占用人工裁决名额。"""
    statements = [
        _statement("F1", "崩溃率上升 12%", "product_agent", ["E1"]),
        _statement("F2", "未发现崩溃异常", "product_agent", ["E2"]),
    ]
    evidence = [
        _doc("E1", doc_id="doc-a", kb="product_kb", name="甲"),
        _doc("E2", doc_id="doc-b", kb="engineering_kb", name="乙"),
    ]

    assert detect_conflicts(statements, evidence) == []


def test_引用同一文档的两条陈述不算冲突():
    statements = [
        _statement("F1", "崩溃率上升 12%", "product_agent", ["E1"]),
        _statement("F2", "未发现崩溃异常", "release_agent", ["E2"]),
    ]
    evidence = [
        _doc("E1", doc_id="doc-same", kb="product_kb", name="同一份文档上"),
        _doc("E2", doc_id="doc-same", kb="engineering_kb", name="同一份文档下"),
    ]

    assert detect_conflicts(statements, evidence) == []


def test_同类型普通文档之间不做冲突判定():
    """双方都是普通文档且没有任何指标证据参与时，视为普通表述差异。"""
    statements = [
        _statement("F1", "崩溃率上升 12%", "product_agent", ["E1"]),
        _statement("F2", "未发现崩溃异常", "release_agent", ["E2"]),
    ]
    evidence = [
        _doc("E1", doc_id="doc-a", kb="document", name="甲"),
        _doc("E2", doc_id="doc-b", kb="document", name="乙"),
    ]

    assert detect_conflicts(statements, evidence) == []


def test_指标证据参与时同一对陈述判为冲突():
    """把一侧换成指标证据后，来源类型不同，冲突成立。"""
    statements = [
        _statement("F1", "崩溃率上升 12%", "metric_agent", ["M1"]),
        _statement("F2", "未发现崩溃异常", "release_agent", ["E2"]),
    ]
    evidence = [
        {
            "evidence_id": "M1",
            "source_kind": "metric",
            "metric_name": "crash_rate",
            "source_name": "指标服务:crash_rate",
            "content": "崩溃率趋势",
        },
        _doc("E2", doc_id="doc-b", kb="engineering_kb", name="发布记录"),
    ]

    conflicts = detect_conflicts(statements, evidence)

    assert len(conflicts) == 1
    assert "metric" in conflicts[0]["source_kinds"]


def test_指标与文档冲突优先呈现():
    """同为冲突时，有指标参与的那条排在前面（信息量最高）。"""
    statements = [
        _statement("F1", "崩溃率上升 12%", "product_agent", ["E1"]),
        _statement("F2", "未发现崩溃异常", "release_agent", ["E2"]),
        _statement("F3", "崩溃率上升 20%", "metric_agent", ["M1"]),
        _statement("F4", "未见崩溃上升", "feedback_agent", ["E3"]),
    ]
    evidence = [
        _doc("E1", doc_id="doc-a", kb="product_kb", name="甲"),
        _doc("E2", doc_id="doc-b", kb="engineering_kb", name="乙"),
        {
            "evidence_id": "M1",
            "source_kind": "metric",
            "metric_name": "crash_rate",
            "source_name": "指标服务:crash_rate",
        },
        _doc("E3", doc_id="doc-c", kb="feedback_kb", name="丙"),
    ]

    conflicts = detect_conflicts(statements, evidence)

    assert conflicts, "应当至少检出一条冲突"
    assert "metric" in conflicts[0]["source_kinds"]


# --------------------------------------------------------------------------- 其他约束
def test_非事实类陈述不参与冲突检测():
    statements = [
        _statement("I1", "崩溃率上升 12%", "product_agent", ["E1"], type_="inference"),
        _statement("F2", "未发现崩溃异常", "release_agent", ["E2"]),
    ]
    evidence = [
        _doc("E1", doc_id="doc-a", kb="product_kb", name="甲"),
        _doc("E2", doc_id="doc-b", kb="engineering_kb", name="乙"),
    ]

    assert detect_conflicts(statements, evidence) == []


def test_补充检索得到的陈述不参与冲突检测():
    statements = [
        _statement("F1", "崩溃率上升 12%", "product_agent", ["E1"]),
        {**_statement("F2", "未发现崩溃异常", "release_agent", ["E2"]), "supplemental": True},
    ]
    evidence = [
        _doc("E1", doc_id="doc-a", kb="product_kb", name="甲"),
        _doc("E2", doc_id="doc-b", kb="engineering_kb", name="乙"),
    ]

    assert detect_conflicts(statements, evidence) == []


def test_无主题词的陈述被忽略():
    statements = [
        _statement("F1", "整体体验有待提升", "product_agent", ["E1"]),
        _statement("F2", "未发现任何问题", "release_agent", ["E2"]),
    ]
    evidence = [
        _doc("E1", doc_id="doc-a", kb="product_kb", name="甲"),
        _doc("E2", doc_id="doc-b", kb="engineering_kb", name="乙"),
    ]

    assert detect_conflicts(statements, evidence) == []


def test_冲突数量封顶():
    """同一主题、两两组合会把候选撑到 10 条，最终只保留 MAX_CONFLICTS 条。"""
    statements: list[dict] = []
    evidence: list[dict] = []
    for index in range(5):
        statements.append(_statement(f"A{index}", "崩溃率上升 12%", f"agent_{index}", [f"EA{index}"]))
        statements.append(
            _statement(f"N{index}", "未发现崩溃异常", f"agent_{index}", [f"EN{index}"])
        )
        evidence.append(_doc(f"EA{index}", doc_id=f"doc-a{index}", kb="product_kb", name=f"甲{index}"))
        evidence.append(
            _doc(f"EN{index}", doc_id=f"doc-b{index}", kb="engineering_kb", name=f"乙{index}")
        )

    conflicts = detect_conflicts(statements, evidence)

    assert len(conflicts) == MAX_CONFLICTS
    assert [c["id"] for c in conflicts] == [f"conflict-{i}" for i in range(1, MAX_CONFLICTS + 1)]
    # 同一对来源只留一条，避免同一处矛盾反复刷屏
    pairs = {tuple(sorted(c["detected_by"])) for c in conflicts}
    assert len(pairs) == len(conflicts)
    assert all(c["topic"] == CRASH for c in conflicts)


def test_空输入返回空列表():
    assert detect_conflicts([], []) == []
