"""模型灰度（canary）的单元测试。

灰度最容易做错的地方不是"能不能切模型"，而是：
1. **不稳定**：同一个任务一会儿走灰度一会儿走对照，结论无法归因；
2. **不可复算**：出问题时算不出某个任务本该走哪一侧；
3. **两边都糊**：没有任务标识的调用随机落进灰度，离线复现结果和线上不一致；
4. **没有退路**：灰度模型挂了直接掉到确定性归纳，而不是退回对照模型。

下面的用例就是把这四条钉死。
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.model_gateway import ModelGateway


def make_gateway(**overrides) -> ModelGateway:
    """与开发者本地 .env 完全隔离的配置（理由见 test_model_routing.py）。"""
    base = {
        "llm_provider": "openai_compatible",
        "llm_base_url": "https://api.deepseek.com/v1",
        "llm_api_key": "sk-global-key",
        "llm_model": "deepseek-chat",
        "llm_temperature": 0.2,
        "llm_max_output_tokens": 4000,
        "agent_model_overrides": {},
        "model_canary": {},
        "model_canary_salt": "pic-canary-1",
    }
    base.update(overrides)
    return ModelGateway(Settings(_env_file=None, **base))


CANARY = {"report_agent": {"model": "deepseek-reasoner-v2", "percent": 100}}


def test_未配置灰度时走常规路由():
    gateway = make_gateway(agent_model_overrides={"report_agent": {"model": "deepseek-reasoner"}})

    profile = gateway.profile_for("report_agent", sample_key="task-1")

    assert profile.model == "deepseek-reasoner"
    assert profile.source == "override"
    assert gateway.canary_for("report_agent", "task-1") is None
    assert gateway.canary_table() == []


def test_灰度百分之百时全量走灰度模型():
    gateway = make_gateway(model_canary=CANARY, agent_model_overrides={"report_agent": {"model": "deepseek-reasoner"}})

    profile = gateway.profile_for("report_agent", sample_key="task-1")

    assert profile.model == "deepseek-reasoner-v2"
    assert profile.source == "canary"


def test_灰度百分比为零等价于关闭():
    gateway = make_gateway(model_canary={"report_agent": {"model": "deepseek-reasoner-v2", "percent": 0}})

    for task_id in ("t-1", "t-2", "t-3"):
        assert gateway.profile_for("report_agent", sample_key=task_id).source == "global"
    assert gateway.canary_table()[0]["enabled"] is False


def test_按任务分桶且同一任务稳定不变():
    gateway = make_gateway(model_canary={"report_agent": {"model": "m-canary", "percent": 50}})

    first = gateway.profile_for("report_agent", sample_key="task-stable").source
    for _ in range(50):
        assert gateway.profile_for("report_agent", sample_key="task-stable").source == first
    # 复算用的桶号也必须是稳定的（排查时靠它判断"本该走哪侧"）
    assert gateway.canary_bucket("task-stable") == gateway.canary_bucket("task-stable")


def test_同一任务的多个agent走同一侧():
    """任务级灰度：一个任务里的所有 Agent 要么都在灰度、要么都在对照。"""
    gateway = make_gateway(
        model_canary={
            "report_agent": {"model": "m-canary", "percent": 50},
            "product_agent": {"model": "m-canary", "percent": 50},
        }
    )

    for task_id in ("t-a", "t-b", "t-c", "t-d"):
        sides = {
            gateway.profile_for(key, sample_key=task_id).source
            for key in ("report_agent", "product_agent")
        }
        assert len(sides) == 1, f"{task_id} 的 Agent 分到了不同侧：{sides}"


def test_分桶比例接近配置值():
    gateway = make_gateway(model_canary={"report_agent": {"model": "m-canary", "percent": 30}})

    hits = sum(
        1
        for i in range(600)
        if gateway.profile_for("report_agent", sample_key=f"task-{i}").source == "canary"
    )

    # 600 个样本、p=0.3 时标准差约 1.9%，留足余量避免偶发抖动
    assert 0.25 <= hits / 600 <= 0.35, f"灰度比例偏离过大: {hits}/600"


def test_没有任务标识时不灰度():
    """离线脚本/启动自检没有 task_id：必须固定在对照侧，否则结果无法复现。"""
    gateway = make_gateway(model_canary={"report_agent": {"model": "m-canary", "percent": 50}})

    assert gateway.profile_for("report_agent").source == "global"
    assert gateway.profile_for("report_agent", sample_key=None).source == "global"


def test_灰度只覆盖显式给出的字段():
    gateway = make_gateway(
        agent_model_overrides={
            "report_agent": {
                "model": "deepseek-reasoner",
                "base_url": "https://api.deepseek.com/v1",
                "temperature": 0.1,
                "max_output_tokens": 8000,
            }
        },
        model_canary={
            "report_agent": {
                "model": "qwen-max",
                "percent": 100,
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key": "sk-canary",
            }
        },
    )

    profile = gateway.profile_for("report_agent", sample_key="t")

    assert profile.model == "qwen-max"
    assert profile.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert profile.api_key == "sk-canary"
    # 灰度没提的参数沿用常规路由
    assert profile.temperature == 0.1
    assert profile.max_output_tokens == 8000


def test_换盐值会重新分桶():
    ids = [f"task-{i}" for i in range(200)]
    a = make_gateway(model_canary={"report_agent": {"model": "m", "percent": 50}})
    b = make_gateway(
        model_canary={"report_agent": {"model": "m", "percent": 50}},
        model_canary_salt="pic-canary-2",
    )

    moved = sum(1 for task_id in ids if a.canary_bucket(task_id) != b.canary_bucket(task_id))

    assert moved > len(ids) * 0.5, "换盐值应当重新分配绝大多数任务的分桶"


def test_灰度链先灰度再退回对照模型():
    gateway = make_gateway(
        llm_fallback_models=["deepseek-chat"],
        agent_model_overrides={"report_agent": {"model": "deepseek-reasoner"}},
        model_canary={"report_agent": {"model": "qwen-max", "percent": 100}},
    )

    chain = gateway.chain_for("report_agent", sample_key="t")

    assert [item.model for item in chain] == ["qwen-max", "deepseek-chat"]
    assert chain[0].source == "canary"
    assert chain[1].source == "canary+fallback"


def test_灰度自带的备用模型排在常规备用模型之前():
    gateway = make_gateway(
        llm_fallback_models=["deepseek-chat"],
        model_canary={
            "report_agent": {
                "model": "qwen-max",
                "percent": 100,
                "fallback_models": ["qwen-plus"],
            }
        },
    )

    chain = gateway.chain_for("report_agent", sample_key="t")

    assert [item.model for item in chain] == ["qwen-max", "qwen-plus", "deepseek-chat"]


def test_非法灰度比例不影响主流程():
    """配置写错（percent 不是数字）时：不灰度、不抛异常，只记警告。"""
    gateway = make_gateway(model_canary={"report_agent": {"model": "m-canary", "percent": "abc"}})

    profile = gateway.profile_for("report_agent", sample_key="t")

    assert profile.source == "global"
    assert profile.model == "deepseek-chat"


def test_灰度描述不含密钥():
    gateway = make_gateway(
        model_canary={
            "report_agent": {"model": "qwen-max", "percent": 20, "api_key": "sk-canary-secret"}
        }
    )

    table = gateway.canary_table()

    assert table[0]["key"] == "report_agent"
    assert table[0]["percent"] == 20
    assert table[0]["has_api_key"] is True
    assert "sk-canary-secret" not in str(table)
    assert "sk-canary-secret" not in str(gateway.profile_for("report_agent", sample_key="t").describe())

