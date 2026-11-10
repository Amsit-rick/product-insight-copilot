"""多模型路由的单元测试（按 Agent / 节点覆盖模型目标）。

覆盖点：
- 未配置覆盖时，所有 Agent 落回全局默认模型；
- 配置覆盖后，只有被覆盖的 Agent 用独立模型，其余保持全局；
- 跨供应商路由：base_url 与 api_key 也能按 Agent 覆盖；
- 没有密钥时一律判定为不可用（走 stub 降级），而不是发出注定失败的请求；
- 对外描述里**不能出现密钥**。
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.model_gateway import ModelGateway


def make_gateway(**overrides) -> ModelGateway:
    """构造一个与开发者本地 .env 完全隔离的配置。

    `_env_file=None` 是必须的：pydantic-settings 的优先级是
    「显式传参 > 环境变量 > .env > 默认值」，只传部分字段时，
    其余字段会从本地 .env 读进来（例如真实的 AGENT_MODEL_OVERRIDES），
    测试结果就会随开发者机器上的配置漂移。
    """
    base = {
        "llm_provider": "openai_compatible",
        "llm_base_url": "https://api.deepseek.com/v1",
        "llm_api_key": "sk-global-key",
        "llm_model": "deepseek-chat",
        "llm_temperature": 0.2,
        "llm_max_output_tokens": 4000,
        "agent_model_overrides": {},
    }
    base.update(overrides)
    return ModelGateway(Settings(_env_file=None, **base))


def test_未配置覆盖时所有agent用全局模型():
    gateway = make_gateway()

    for code in ("product_agent", "feedback_agent", "release_agent", "metric_agent", "report_agent"):
        profile = gateway.profile_for(code)
        assert profile.model == "deepseek-chat"
        assert profile.base_url == "https://api.deepseek.com/v1"
        assert profile.source == "global"
        assert profile.configured is True


def test_按agent覆盖模型且不影响其他agent():
    gateway = make_gateway(
        agent_model_overrides={"report_agent": {"model": "deepseek-reasoner"}},
    )

    report = gateway.profile_for("report_agent")
    assert report.model == "deepseek-reasoner"
    assert report.source == "override"
    assert report.base_url == "https://api.deepseek.com/v1"  # 未覆盖的字段继承全局

    assert gateway.profile_for("metric_agent").model == "deepseek-chat"
    assert gateway.profile_for("metric_agent").source == "global"


def test_跨供应商路由可覆盖端点与密钥():
    gateway = make_gateway(
        agent_model_overrides={
            "feedback_agent": {
                "model": "qwen-max",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "api_key": "sk-qwen-key",
            }
        }
    )

    qwen = gateway.profile_for("feedback_agent")
    assert qwen.model == "qwen-max"
    assert qwen.base_url.endswith("compatible-mode/v1")
    assert qwen.api_key == "sk-qwen-key"
    assert qwen.configured is True


def test_节点级覆盖也可用():
    gateway = make_gateway(
        agent_model_overrides={"intent_classifier": {"model": "qwen-turbo"}},
    )

    assert gateway.profile_for("intent_classifier").model == "qwen-turbo"
    assert gateway.profile_for("answerability_check").model == "deepseek-chat"


def test_缺少密钥时判定为不可用并降级():
    gateway = make_gateway(llm_api_key="")

    profile = gateway.profile_for("report_agent")
    assert profile.configured is False
    assert gateway.enabled is False

    payload, usage = gateway.chat_json(
        "system", "user", fallback=lambda: {"ok": True}, profile=profile
    )
    assert payload == {"ok": True}
    assert usage.stub is True
    assert usage.error == "model_not_configured"
    # 即使不可用，也要如实记录"本来打算用哪个模型"
    assert usage.model == "deepseek-chat"


def test_provider为stub时不发请求():
    gateway = make_gateway(llm_provider="stub")

    assert gateway.profile_for("report_agent").configured is False
    assert gateway.enabled is False


def test_对外描述不泄露密钥():
    gateway = make_gateway(
        agent_model_overrides={"report_agent": {"model": "qwen-max", "api_key": "sk-secret-value"}}
    )

    described = gateway.profile_for("report_agent").describe()
    assert "api_key" not in described
    assert "sk-secret-value" not in str(described)
    assert described["model"] == "qwen-max"


def test_未配置时对外描述显示stub():
    gateway = make_gateway(llm_api_key="")

    described = gateway.profile_for("report_agent").describe()
    assert described["model"] == "stub"
    assert described["base_url"] is None
    assert described["configured"] is False


def test_路由表覆盖五个agent():
    gateway = make_gateway(agent_model_overrides={"metric_agent": {"model": "glm-4-flash"}})

    table = {item["key"]: item["model"] for item in gateway.routing_table()}
    assert set(table) == {
        "product_agent",
        "feedback_agent",
        "release_agent",
        "metric_agent",
        "report_agent",
    }
    assert table["metric_agent"] == "glm-4-flash"
    assert table["product_agent"] == "deepseek-chat"


@pytest.mark.parametrize(
    "override",
    [
        {"model": "qwen-max", "temperature": 0.7},
        {"model": "glm-4-plus", "max_output_tokens": 1024},
        {"model": "deepseek-reasoner", "timeout_seconds": 120, "max_retries": 3},
    ],
)
def test_覆盖推理参数会生效(override: dict):
    gateway = make_gateway(agent_model_overrides={"product_agent": override})

    profile = gateway.profile_for("product_agent")
    assert profile.model == override["model"]
    if "temperature" in override:
        assert profile.temperature == override["temperature"]
    if "max_output_tokens" in override:
        assert profile.max_output_tokens == override["max_output_tokens"]
    if "timeout_seconds" in override:
        assert profile.timeout_seconds == override["timeout_seconds"]
        assert profile.max_retries == override["max_retries"]


# --------------------------------------------------------------------------- 备用模型链（降级）
def test_模型链包含主模型与全局备用模型():
    gateway = make_gateway(llm_fallback_models=["backup-1", "backup-2"])

    chain = gateway.chain_for("report_agent")

    assert [profile.model for profile in chain] == ["deepseek-chat", "backup-1", "backup-2"]
    assert chain[0].source == "global"
    assert "fallback" in chain[1].source


def test_按agent配置的备用模型优先且去重():
    gateway = make_gateway(
        llm_fallback_models=["global-backup", "deepseek-chat"],
        agent_model_overrides={
            "report_agent": {"model": "primary", "fallback_models": ["agent-backup", "primary"]}
        },
    )

    chain = gateway.chain_for("report_agent")

    # 顺序：按 Agent 的备用 > 全局备用；重复项（含与主模型同名）会被去掉
    # 注意 "deepseek-chat" 虽然与"全局默认模型"同名，但这里主模型是 primary，
    # 因此它作为全局备用仍然有效，会被保留。
    assert [profile.model for profile in chain] == [
        "primary",
        "agent-backup",
        "global-backup",
        "deepseek-chat",
    ]


def test_主模型失败时自动降级到备用模型(monkeypatch):
    from app.services.model_gateway import LLMUsage, ModelError

    gateway = make_gateway(
        agent_model_overrides={"report_agent": {"model": "primary", "fallback_models": ["backup"]}}
    )
    seen: list[str] = []

    def fake_chat(system, user, *, max_tokens=None, temperature=None, profile=None):  # noqa: ANN001
        seen.append(profile.model)
        if profile.model == "primary":
            raise ModelError("primary 不可用")
        return '{"summary": "来自备用模型"}', LLMUsage(model=profile.model, prompt_tokens=10, completion_tokens=5)

    monkeypatch.setattr(gateway, "chat", fake_chat)
    payload, usage = gateway.chat_json(
        "system",
        "user",
        fallback=lambda: {"summary": "确定性回退"},
        profile=gateway.profile_for("report_agent"),
        chain=gateway.chain_for("report_agent"),
    )

    assert payload["summary"] == "来自备用模型"
    assert usage.model == "backup"
    assert usage.stub is False
    assert "primary" in (usage.error or ""), "应记录主模型失败原因，便于排查"
    # 主模型会先按 retries 重试一次，再切到备用模型（顺序可预期）
    assert seen == ["primary", "primary", "backup"]


def test_所有模型都失败时回退到确定性结果并保留token用量(monkeypatch):
    from app.services.model_gateway import LLMUsage, ModelError

    gateway = make_gateway(
        agent_model_overrides={"report_agent": {"model": "primary", "fallback_models": ["backup"]}}
    )

    def fake_chat(system, user, *, max_tokens=None, temperature=None, profile=None):  # noqa: ANN001
        raise ModelError(f"{profile.model} 挂了")

    monkeypatch.setattr(gateway, "chat", fake_chat)
    payload, usage = gateway.chat_json(
        "system",
        "user",
        fallback=lambda: {"summary": "确定性回退"},
        profile=gateway.profile_for("report_agent"),
        chain=gateway.chain_for("report_agent"),
    )

    assert payload == {"summary": "确定性回退"}
    assert usage.stub is True
    assert "primary" in usage.error and "backup" in usage.error
    # 关键是：即便全部失败，也要如实记录"本来打算用哪个模型"
    assert usage.model == "primary"


# --------------------------------------------------------------------------- 并发闸门
def test_并发额度被占满时按调用失败降级():
    from app.services.model_gateway import ModelGateway

    gateway = make_gateway(model_max_concurrency=1, model_concurrency_timeout_seconds=0.05)
    semaphore = gateway._semaphore()
    assert semaphore.acquire(timeout=1) is True
    try:
        content, usage = gateway.chat("s", "u", profile=gateway.profile_for("report_agent"))
    finally:
        semaphore.release()

    assert content == ""
    assert usage.stub is True
    assert usage.error == "model_concurrency_timeout"


def test_相同并发配置共享同一个信号量():
    from app.services.model_gateway import ModelGateway

    gateway_a = make_gateway(model_max_concurrency=3)
    gateway_b = make_gateway(model_max_concurrency=3)

    assert gateway_a._semaphore() is gateway_b._semaphore()


def test_不同并发配置使用不同信号量():
    gateway_a = make_gateway(model_max_concurrency=2)
    gateway_b = make_gateway(model_max_concurrency=5)

    assert gateway_a._semaphore() is not gateway_b._semaphore()
