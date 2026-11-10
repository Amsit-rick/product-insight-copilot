"""知识库问答状态归一化单测。"""

from types import SimpleNamespace

from app.config import Settings
from app.rag.retrieval import RetrievalHit
from app.services.knowledge_qa import (
    QaAnswer,
    QaCitation,
    _asks_for_complete_list,
    _citation_excerpt,
    _compact_direct_answer,
    _first_sentences,
    _looks_insufficient_answer,
    _model_answer,
    _point_of,
    _query_excerpt,
    _structured_list_answer,
)


def test_模型明确说材料不足时必须识别为拒答() -> None:
    assert _looks_insufficient_answer("检索到的材料不足以回答该问题。") is True
    assert _looks_insufficient_answer("当前信息不足以回答 login_success 的完整口径。") is True
    assert _looks_insufficient_answer("根据引用片段，login_success 在登录成功后上报。") is False


def test_证据不足免责说明不再声称引用就是结论() -> None:
    answer = QaAnswer(query="q", answer="材料不足以回答", insufficient=True)
    payload = answer.as_dict()
    assert "仅供核对" in payload["disclaimer"]
    assert "结论直接来自下方引用片段" not in payload["disclaimer"]


def test_引用摘要不能被数字章节标题截成一个编号() -> None:
    content = (
        "2. 核心事件\n"
        "| 事件名 | 触发时机 | 关键字段 | 主要用途 |\n"
        "| `user_login` | 登录流程的最终状态 | `result`、`fail_code`、`duration_ms` | 登录成功率 |\n"
        "| `app_launch` | 应用进入前台 | `launch_type`、`first_launch` | 活跃统计与崩溃率分母 |"
    )

    snippet = _first_sentences(content, max_chars=80)

    assert snippet != "2."
    assert "user_login" in snippet


def test_引用摘要应围绕问题中的精确事件名截取() -> None:
    content = (
        "2. 核心事件 "
        + "前置事件说明 " * 40
        + "| `user_login` | 登录流程的最终状态 | `result`、`fail_code`、`duration_ms` | 登录成功率 |"
    )

    snippet = _query_excerpt(
        content,
        "埋点字典里 user_login 事件的口径是怎么定义的？",
        max_chars=160,
    )

    assert "user_login" in snippet
    assert "登录流程的最终状态" in snippet


def test_模型归纳应看到完整命中切片而不是前端截断摘要(monkeypatch) -> None:
    content = (
        "3. 数据质量问题与处理约定（重要） "
        "1. 2026-08-25 整日缺数，趋势对比时跳过该日或改用 7 日均值。"
        "2. 2026-08-24 低端机小样本仅 312，分析时应剔除。"
        "3. 2026-08-26 存在重复行，按业务键去重并保留最新值。"
        "4. iOS 不产出 low_end 分群，跨平台比较必须对齐 high_end。"
    )
    captured: dict[str, str] = {}

    class Gateway:
        def profile_for(self, _key: str):
            return SimpleNamespace(configured=True)

        def chat_json(self, _system: str, user: str, **_kwargs):
            captured["user"] = user
            return (
                {"answer": "四项约定均已覆盖", "used_citations": [1]},
                SimpleNamespace(stub=False, model="test-model"),
            )

    monkeypatch.setattr("app.services.model_gateway.get_model_gateway", lambda: Gateway())
    hit = RetrievalHit(
        chunk_id="chunk-1",
        document_id="doc-1",
        kb_code="operation_kb",
        content=content,
        source_name="指标口径与报表说明.md",
        page=None,
        document_version="2.1.0",
        updated_at=None,
        relevance_score=0.9,
    )
    citation = QaCitation(
        index=1,
        chunk_id="chunk-1",
        document_id="doc-1",
        kb_code="operation_kb",
        source_name="指标口径与报表说明.md",
        page=None,
        document_version="2.1.0",
        score=0.9,
        snippet=content[:120],
    )

    result = _model_answer(
        "数据质量问题与处理约定是怎么做的？",
        [hit],
        Settings(),
        [citation],
    )

    assert result == ("四项约定均已覆盖。", "test-model")
    assert "2026-08-26" in captured["user"]
    assert "iOS 不产出 low_end" in captured["user"]


def test_模型直接答案不能把多个原文片段整段拼接给用户(monkeypatch) -> None:
    copied_answer = (
        "报表使用的注意事项包括：日报 T+1 09:00 刷新，数据来源为指标快照 metric_snapshot_20260831，"
        "导出文件与看板同源；分群样本量 < 500 的单元格在报表中留空，原始明细仍保留数值；"
        "注意二次口径差异，本部门 crash_rate 含 native 崩溃，研发崩溃看板仅含 Java 层崩溃；"
        "报表导出文件可能存在重复行与缺日，导入前请执行去重与补数检查；"
        "数据质量方面：2026-08-25 整日缺数，趋势对比时应跳过该日；"
        "2026-08-26 出现重复行，应按 metric_name、platform、app_version 等字段去重。"
    )

    class Gateway:
        def profile_for(self, _key: str):
            return SimpleNamespace(configured=True)

        def chat_json(self, _system: str, _user: str, **_kwargs):
            return (
                {"answer": copied_answer, "used_citations": [1, 2]},
                SimpleNamespace(stub=False, model="test-model"),
            )

    monkeypatch.setattr("app.services.model_gateway.get_model_gateway", lambda: Gateway())
    hits = [
        RetrievalHit(
            chunk_id="chunk-1",
            document_id="doc-1",
            kb_code="operation_kb",
            content="报表刷新与使用注意。",
            source_name="指标口径与报表说明.md",
            page=None,
            document_version="2.1.0",
            updated_at=None,
            relevance_score=0.9,
        )
    ]
    citations = [
        QaCitation(
            index=1,
            chunk_id="chunk-1",
            document_id="doc-1",
            kb_code="operation_kb",
            source_name="指标口径与报表说明.md",
            page=None,
            document_version="2.1.0",
            score=0.9,
            snippet="报表刷新与使用注意。",
        )
    ]

    result = _model_answer("报表使用有哪些注意事项？", hits, Settings(), citations)

    assert result is not None
    answer, _model = result
    assert len(answer) <= 280
    assert "去重与补数检查" in answer
    assert "2026-08-25" not in answer
    assert "metric_name、platform、app_version" not in answer


def test_直接答案硬上限包含自动补充的句号() -> None:
    answer = _compact_direct_answer("说明" * 300)

    assert len(answer) <= 280
    assert answer.endswith("。")


def test_纯检索要点应保留同一节中的四条并列约定() -> None:
    content = (
        "3. 数据质量问题与处理约定（重要）\n"
        "1. **2026-08-25 整日缺数**：上游明细任务调度失败，当日分区未产出，报表与导出文件整天缺失，"
        "未做回补。做趋势对比时应跳过该日或改用 7 日均值。\n"
        "2. **2026-08-24 低端机小样本**：Android 3.2.0 + channel_a + low_end 当日样本仅 **312**"
        "（低于 500 阈值），该数值不具统计显著性，分析时应剔除该点。\n"
        "3. **2026-08-26 重复行**：报表任务重跑，导致 Android 3.2.0 + channel_a + low_end "
        "出现两条维度完全相同、取值略有差异的记录（0.223 与 0.217）。导入时应按 "
        "`(metric_name, platform, app_version, channel, device, stat_date)` 去重，保留任务重跑后的最新值。\n"
        "4. **iOS 不产出 low_end 分群**：iOS 侧该分群为缺失而非 0，跨平台比较时必须对齐到 high_end 分群。"
    )
    hit = RetrievalHit(
        chunk_id="chunk-1",
        document_id="doc-1",
        kb_code="operation_kb",
        content=content,
        source_name="指标口径与报表说明.md",
        page=None,
        document_version="2.1.0",
        updated_at=None,
        relevance_score=0.9,
        section_title="3. 数据质量问题与处理约定（重要）",
    )

    point = _point_of(hit)

    assert "2026-08-26" in point
    assert "iOS 不产出 low_end" in point


def test_宽泛的有哪些和注意事项不能劫持到任意编号清单() -> None:
    assert _asks_for_complete_list("报表使用有哪些注意事项？") is False
    assert _asks_for_complete_list("数据质量问题与处理约定是怎么做的？") is True


def test_清单引用应按编号项完整重组而不是按固定字数截断() -> None:
    long_text = "3. 数据质量处理约定\n" + "\n".join(
        f"{index}. 第{index}条约定：" + (f"内容{index}" * 90)
        for index in range(1, 5)
    )

    snippet = _citation_excerpt(long_text, "数据质量处理约定是什么？")

    assert "1. 第1条约定" in snippet
    assert "4. 第4条约定" in snippet
    assert snippet.endswith("内容4")


def test_清单直接答案应归纳处理动作而不是逐字复制原文() -> None:
    content = (
        "3. 数据质量问题与处理约定（重要）\n"
        "1. **2026-08-25 整日缺数**：上游明细任务调度失败，当日分区未产出，报表与导出文件整天缺失，"
        "未做回补。做趋势对比时应跳过该日或改用 7 日均值。\n"
        "2. **2026-08-24 低端机小样本**：Android 3.2.0 + channel_a + low_end 当日样本仅 **312**"
        "（低于 500 阈值），该数值不具统计显著性，分析时应剔除该点。\n"
        "3. **2026-08-26 重复行**：报表任务重跑，出现两条维度相同、取值略有差异的记录。"
        "导入时应按 `(metric_name, platform, app_version, channel, device, stat_date)` 去重，保留最新值。\n"
        "4. **iOS 不产出 low_end 分群**：iOS 侧该分群为缺失而非 0，跨平台比较时必须对齐到 high_end 分群。"
    )
    hit = RetrievalHit(
        chunk_id="chunk-1",
        document_id="doc-1",
        kb_code="operation_kb",
        content=content,
        source_name="指标口径与报表说明.md",
        page=None,
        document_version="2.1.0",
        updated_at=None,
        relevance_score=0.9,
        section_title="3. 数据质量问题与处理约定（重要）",
    )

    answer = _structured_list_answer("数据质量问题与处理约定是怎么做的？", [hit])

    assert answer is not None
    assert "1. 2026-08-25 整日缺数：做趋势对比时应跳过该日或改用 7 日均值" in answer
    assert "2. 2026-08-24 低端机小样本" in answer and "分析时应剔除该点" in answer
    assert "3. 2026-08-26 重复行：导入时应" in answer
    assert "4. iOS 不产出 low_end 分群" in answer and "必须对齐到 high_end" in answer
    assert "上游明细任务调度失败" not in answer
    assert "(metric_name, platform, app_version" not in answer
