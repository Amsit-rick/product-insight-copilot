"""模型网关（ModelGateway）与向量化客户端（EmbeddingClient）。

设计文档 6 节：模型调用使用原生 SDK / HTTP 接口，不绑定 LangChain；
DeepSeek / Qwen / GLM 通过 OpenAI 兼容协议切换，密钥只从服务端环境变量读取。

关键点：本模块提供 `fallback` 机制——当模型不可用或未配置时，
调用方传入的确定性回退结果会被使用，并在返回的 usage 中标记 `stub=True`。
这保证：主流程永远可运行，且"是否用了真模型"在 trace 里一目了然。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

import httpx
import numpy as np

from app.config import Settings, get_settings
from app.logging_utils import get_logger
from app.services.caching import note_lookup

logger = get_logger(__name__)

_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


@dataclass
class LLMUsage:
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 命中供应商侧 prompt 缓存的那部分输入 token（计费单价更低）
    cache_hit_tokens: int = 0
    latency_ms: int = 0
    stub: bool = False
    error: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class ModelError(Exception):
    pass


@dataclass(frozen=True)
class ModelProfile:
    """一次模型调用的完整目标描述（由全局配置 + 按 Agent 覆盖解析而来）。

    为什么要把"模型"抽象成 profile 而不是只传一个模型名：
    多模型路由的典型场景是**跨供应商**（报告 Agent 用 DeepSeek、反馈聚类用 Qwen），
    因此端点与密钥也必须能按 Agent 覆盖，否则只能在同一家厂商内换型号。
    """

    key: str  # agent_code（如 report_agent）或节点名（如 intent_classifier）
    model: str
    base_url: str
    api_key: str
    provider: str
    temperature: float
    max_output_tokens: int
    timeout_seconds: int
    max_retries: int
    source: str = "global"  # global | override

    @property
    def configured(self) -> bool:
        """只有 provider 打开且拿到密钥，才算真正可用（否则走 stub 降级）。"""
        return self.provider == "openai_compatible" and bool(self.api_key)

    def describe(self) -> dict:
        """对外描述（**不含密钥**），用于 /health、/agents 与前端展示。"""
        return {
            "key": self.key,
            "model": self.model if self.configured else "stub",
            "base_url": self.base_url if self.configured else None,
            "provider": self.provider,
            "configured": self.configured,
            "source": self.source,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
        }


class ModelGateway:
    """统一的聊天补全入口。同步实现，由后台线程调用。

    路由规则（`profile_for`）：
        按 Agent 覆盖  >  全局默认配置
    因此既能"整个平台一个模型"，也能"报告 Agent 用强模型、其他 Agent 用便宜模型"，
    未配置覆盖的 Agent 自动落回全局模型。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._clients: dict[str, httpx.Client] = {}
        self._stats_lock = threading.Lock()
        self._stats: dict[str, int] = {
            "chat_calls": 0,
            "chat_json_calls": 0,
            "json_parse_failures": 0,
            "json_retries": 0,
            "fallbacks": 0,
        }

    def _bump_stat(self, key: str) -> None:
        with self._stats_lock:
            self._stats[key] = self._stats.get(key, 0) + 1

    def stats(self) -> dict[str, int]:
        """运行期模型健康计数；供 /health 与系统页展示，不包含 Prompt/输出。"""
        with self._stats_lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._stats_lock:
            for key in self._stats:
                self._stats[key] = 0

    # ------------------------------------------------------------------ 基础调用
    def _http(self, profile: ModelProfile) -> httpx.Client:
        # 按 base_url 复用连接池：跨供应商路由时不同端点各有各的连接
        client = self._clients.get(profile.base_url)
        if client is None:
            client = httpx.Client(timeout=profile.timeout_seconds)
            self._clients[profile.base_url] = client
        return client

    # ------------------------------------------------------------------ 路由解析
    def canary_bucket(self, sample_key: str) -> int:
        """把一次流量稳定地映射到 0-99 的分桶（同一个 task_id 永远同一个桶）。

        用哈希而不是随机数，是为了让"任务级灰度"具备两个性质：
        1. **稳定**：同一任务的多次 Agent 调用、重试、重启后都落在同一侧；
        2. **可复算**：线上排查时用 task_id 就能算出它该走哪一侧，不依赖日志。
        """
        salt = self.settings.model_canary_salt or ""
        digest = hashlib.sha256(f"{salt}:{sample_key}".encode()).digest()
        return int.from_bytes(digest[:4], "big") % 100

    def canary_for(self, key: str | None, sample_key: str | None) -> dict | None:
        """某个 Agent/节点这次是否走灰度模型；返回灰度配置片段或 None。

        - 没有灰度配置 → None（走常规路由）；
        - `percent <= 0` → None（灰度关闭）；
        - `percent >= 100` → 全量走灰度（等于换主模型，仍保留提示）；
        - 没有 `sample_key`（如启动自检、离线脚本）→ 不灰度：宁可走稳定分支，
          也不要让"没有任务上下文的调用"随机落进灰度，否则结果无法复现。
        """
        raw = (self.settings.model_canary or {}).get(key or "") or {}
        if not isinstance(raw, dict) or not raw:
            return None
        try:
            percent = float(raw.get("percent", 0) or 0)
        except (TypeError, ValueError):
            logger.warning("灰度配置 percent 非法，按不灰度处理: key=%s value=%r", key, raw.get("percent"))
            return None
        if percent <= 0 or sample_key is None:
            return None
        if percent >= 100:
            return raw
        return raw if self.canary_bucket(sample_key) < percent else None

    def canary_table(self) -> list[dict]:
        """灰度配置概览（不含密钥），供 /health 与运维排查。"""
        table: list[dict] = []
        for key, raw in (self.settings.model_canary or {}).items():
            if not isinstance(raw, dict):
                continue
            percent = raw.get("percent", 0)
            table.append(
                {
                    "key": key,
                    "model": str(raw.get("model") or self.settings.llm_model),
                    "percent": percent,
                    "enabled": bool(raw) and float(percent or 0) > 0,
                    "has_api_key": bool(raw.get("api_key")),
                    "salt": self.settings.model_canary_salt,
                }
            )
        return table

    def profile_for(self, key: str | None = None, *, sample_key: str | None = None) -> ModelProfile:
        """解析某个 Agent/节点的模型目标。

        `key` 可以是 Agent 代码（product_agent…）、节点名（intent_classifier、
        answerability_check），传 None 表示"只要全局默认"。
        `sample_key` 一般是 task_id：用于任务级灰度分桶（同一个任务稳定走同一侧）。
        优先级：灰度 > 按 Agent 覆盖 > 全局默认。
        """
        settings = self.settings
        override: dict = {}
        if key:
            raw = settings.agent_model_overrides or {}
            candidate = raw.get(key) or {}
            if isinstance(candidate, dict):
                override = candidate

        source = "override" if override else "global"
        if key:
            canary = self.canary_for(key, sample_key)
            if canary:
                # 灰度只覆盖它显式给出的字段（模型/端点/密钥/参数），其余沿用常规路由
                override = {**override, **canary}
                source = "canary"

        base_url = str(override.get("base_url") or settings.llm_base_url or "")
        return ModelProfile(
            key=key or "default",
            model=str(override.get("model") or settings.llm_model),
            base_url=base_url,
            # 密钥只从服务端环境变量/配置读取，绝不进日志与前端
            api_key=str(override.get("api_key") or settings.llm_api_key or ""),
            provider=str(override.get("provider") or settings.llm_provider),
            temperature=float(override.get("temperature", settings.llm_temperature)),
            max_output_tokens=int(
                override.get("max_output_tokens", settings.llm_max_output_tokens)
            ),
            timeout_seconds=int(override.get("timeout_seconds", settings.llm_timeout_seconds)),
            max_retries=int(override.get("max_retries", settings.llm_max_retries)),
            source=source,
        )

    @property
    def enabled(self) -> bool:
        """全局默认模型是否可用（保留旧语义，供 /health 等处使用）。"""
        return self.profile_for(None).configured

    def routing_table(self, keys: list[str] | None = None) -> list[dict]:
        """给 /agents、/health 用的路由表（不含密钥）。"""
        codes = keys or [
            "product_agent",
            "feedback_agent",
            "release_agent",
            "metric_agent",
            "report_agent",
        ]
        return [self.profile_for(code).describe() for code in codes]

    def chain_for(self, key: str | None = None, *, sample_key: str | None = None) -> list[ModelProfile]:
        """返回该 Agent/节点的完整模型链：主模型 → 备用模型（按配置顺序）。

        备用模型沿用主模型的端点与密钥；要跨供应商，请在
        `agent_model_overrides[<key>]` 或全局 `LLM_*` 里另行配置端点与密钥。
        传入 `sample_key`（task_id）时，链首可能是灰度模型——此时备用模型先取灰度自己配的
        `fallback_models`，再落回常规配置，保证"灰度模型挂了"也能退回对照模型而非直接降级到确定性归纳。
        """
        primary = self.profile_for(key, sample_key=sample_key)
        override = (self.settings.agent_model_overrides or {}).get(key or "") or {}
        names: list[str] = []
        canary = self.canary_for(key, sample_key)
        if canary:
            names.extend(canary.get("fallback_models") or [])
        if isinstance(override, dict):
            names.extend(override.get("fallback_models") or [])
        names.extend(self.settings.llm_fallback_models or [])

        chain = [primary]
        seen = {primary.model}
        for name in names:
            model = str(name).strip()
            if not model or model in seen:
                continue
            seen.add(model)
            chain.append(replace(primary, model=model, source=f"{primary.source}+fallback"))
        return chain

    # ------------------------------------------------------------------ 并发闸门
    _semaphores: ClassVar[dict[int, threading.BoundedSemaphore]] = {}
    _semaphore_lock: ClassVar[threading.Lock] = threading.Lock()

    def _semaphore(self) -> threading.BoundedSemaphore:
        """全局共享的模型调用并发闸门（按配置并发数复用同一个信号量）。

        多个 Agent 并行执行时会同时打向模型；不限制并发很容易触发供应商限流，
        或者把本地连接池打满。这里用信号量把并发压在配置值以内。
        """
        limit = max(1, int(self.settings.model_max_concurrency or 1))
        with ModelGateway._semaphore_lock:
            semaphore = ModelGateway._semaphores.get(limit)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(limit)
                ModelGateway._semaphores[limit] = semaphore
            return semaphore

    # ------------------------------------------------------------------ 调用
    def chat(
        self,
        system: str,
        user: str,
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        profile: ModelProfile | None = None,
    ) -> tuple[str, LLMUsage]:
        profile = profile or self.profile_for(None)
        self._bump_stat("chat_calls")
        usage = LLMUsage(model=profile.model)
        if not profile.configured:
            usage.stub = True
            usage.error = "model_not_configured"
            return "", usage

        # 并发闸门：拿不到额度就按"模型调用失败"处理，由上层走降级链
        semaphore = self._semaphore()
        acquired = semaphore.acquire(timeout=self.settings.model_concurrency_timeout_seconds)
        if not acquired:
            usage.stub = True
            usage.error = "model_concurrency_timeout"
            logger.warning(
                "等待模型并发额度超时（上限 %s），本次调用降级", self.settings.model_max_concurrency
            )
            return "", usage

        payload = {
            "model": profile.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": profile.temperature if temperature is None else temperature,
            "max_tokens": max_tokens or profile.max_output_tokens,
        }
        headers = {"Authorization": f"Bearer {profile.api_key}"}
        started = time.perf_counter()
        last_error: str | None = None
        try:
            for attempt in range(profile.max_retries + 1):
                try:
                    resp = self._http(profile).post(
                        f"{profile.base_url.rstrip('/')}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    provider_usage = data.get("usage", {}) or {}
                    usage.prompt_tokens = provider_usage.get("prompt_tokens", 0)
                    usage.completion_tokens = provider_usage.get("completion_tokens", 0)
                    # DeepSeek 等供应商会返回命中 prompt 缓存的 token 数，计费单价更低
                    usage.cache_hit_tokens = int(
                        provider_usage.get("prompt_cache_hit_tokens")
                        or (provider_usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                        or 0
                    )
                    usage.latency_ms = int((time.perf_counter() - started) * 1000)
                    # 保留结束原因等原始信息，便于排查"输出被截断"这类问题
                    usage.raw = {
                        "finish_reason": data["choices"][0].get("finish_reason"),
                        "usage": provider_usage,
                    }
                    return content or "", usage
                except Exception as exc:  # 网络/限流/鉴权都按失败处理
                    last_error = str(exc)
                    logger.warning(
                        "模型调用失败(model=%s, attempt=%s): %s",
                        profile.model,
                        attempt + 1,
                        last_error,
                    )
                    if attempt < profile.max_retries:
                        time.sleep(0.5 * (attempt + 1))
            usage.error = last_error
            usage.latency_ms = int((time.perf_counter() - started) * 1000)
            raise ModelError(last_error or "unknown model error")
        finally:
            semaphore.release()

    # --------------------------------------------------------- 结构化输出（受控）
    def chat_json(
        self,
        system: str,
        user: str,
        *,
        fallback: Callable[[], dict],
        max_tokens: int | None = None,
        retries: int = 1,
        profile: ModelProfile | None = None,
        chain: list[ModelProfile] | None = None,
    ) -> tuple[dict, LLMUsage]:
        """要求模型输出严格 JSON；失败或校验不通过时使用确定性回退。

        降级顺序：**主模型 → 备用模型（依次）→ 确定性归纳**。
        每一级都会把失败原因累计到 `usage.error`，因此"最后为什么没用上模型"是可查的。

        Agent 输出只能作为候选事实，回退结果同样必须经过证据校验，
        因此这里不做"编造"，只做"降级为基于工具结果的确定性归纳"。
        """
        self._bump_stat("chat_json_calls")
        profiles = chain or [profile or self.profile_for(None)]
        schema_instruction = (
            "\n严格要求：只输出一个 JSON 对象，不要包含解释、markdown 代码块或多余文本。"
        )
        errors: list[str] = []
        # 保留最后一次真实调用的用量：即使输出不是合法 JSON，
        # 这次调用也确实花了 token，不能让它凭空消失（成本核算要准）。
        last_usage = LLMUsage(model=profiles[0].model)

        for index, candidate in enumerate(profiles):
            profile = candidate
            if not profile.configured:
                if index == 0:
                    self._bump_stat("fallbacks")
                    return fallback(), LLMUsage(
                        model=profile.model, stub=True, error="model_not_configured"
                    )
                continue
            for attempt in range(retries + 1):
                try:
                    content, usage = self.chat(
                        system + schema_instruction,
                        user,
                        max_tokens=max_tokens,
                        temperature=0,
                        profile=profile,
                    )
                    last_usage = usage
                    data = _parse_json_object(content)
                    if data is not None:
                        if index > 0:
                            usage.error = f"已降级到备用模型；主模型失败原因：{errors[0] if errors else '未知'}"
                            logger.warning(
                                "主模型不可用，已由备用模型回答（model=%s）", profile.model
                            )
                        return data, usage
                    last_error = "模型输出不是合法 JSON"
                    self._bump_stat("json_parse_failures")
                    logger.warning(
                        "模型输出无法解析为 JSON（model=%s, 长度=%s, 结束原因=%s）",
                        profile.model,
                        len(content),
                        usage.raw.get("finish_reason") if isinstance(usage.raw, dict) else None,
                    )
                except ModelError as exc:
                    last_error = str(exc)
                if attempt < retries:
                    self._bump_stat("json_retries")
                    continue
                errors.append(f"{profile.model}: {last_error}")

        self._bump_stat("fallbacks")
        return fallback(), LLMUsage(
            model=last_usage.model or profiles[0].model,
            prompt_tokens=last_usage.prompt_tokens,
            completion_tokens=last_usage.completion_tokens,
            cache_hit_tokens=last_usage.cache_hit_tokens,
            latency_ms=last_usage.latency_ms,
            stub=True,
            error="；".join(errors) or "invalid_json_output",
            raw=last_usage.raw,
        )


def _parse_json_object(text: str) -> dict | None:
    """从模型输出里提取 JSON 对象。

    真实模型常见的三种"不合法"：包在 ``` 里、前后带解释、尾逗号/截断。
    前两种做提取；尾逗号做保守修复；截断或非对象直接返回 None（交给确定性回退）。
    """
    if not text:
        return None
    cleaned = text.strip().lstrip("\ufeff")
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*", "", cleaned).strip()
        cleaned = cleaned.rstrip("`").strip()

    candidates = [cleaned]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidates.append(cleaned[start : end + 1])

    for candidate in candidates:
        for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                data = json.loads(attempt)
            except Exception:
                continue
            if isinstance(data, dict):
                return data
    return None


# --------------------------------------------------------------------- Embedding
class EmbeddingClient:
    """BGE-M3 / 兼容接口优先；未配置时使用确定性哈希向量，保证离线可检索。

    带**文本级向量缓存**：同一次诊断里多个 Agent 会用同一段改写后的查询词去检索
    不同的知识库（`rewrite_query` 只取前 24 个 token，长问题的追加关键词会被截掉），
    于是同一段文本在一个任务里被重复向量化 6 次。离线哈希向量只是白烧 CPU，
    接了真实 Embedding 接口就是**6 次计费调用**。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: httpx.Client | None = None
        self._vectors: OrderedDict[str, tuple[float, list[float]]] = OrderedDict()
        self._vectors_lock = threading.Lock()
        self._cache_hits = 0
        self._cache_misses = 0
        self._requests = 0
        self._failures = 0
        self._fallbacks = 0

    def _vector_cache_key(self, text: str) -> str:
        """键里必须带**模型身份**：换了 embedding 模型/维度后，旧向量绝不能复用。"""
        identity = f"{self.model_name}|{self.dim}"
        digest = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
        return f"{identity}|{digest}"

    def _cache_get(self, text: str) -> list[float] | None:
        if not self._cache_enabled:
            return None
        key = self._vector_cache_key(text)
        now = time.time()
        with self._vectors_lock:
            item = self._vectors.get(key)
            if item is None:
                self._cache_misses += 1
                return None
            expires_at, vector = item
            if expires_at and now > expires_at:
                self._vectors.pop(key, None)
                self._cache_misses += 1
                return None
            self._vectors.move_to_end(key)  # LRU：最近用过的留到最后淘汰
            self._cache_hits += 1
            return vector

    def _cache_put(self, text: str, vector: list[float]) -> None:
        if not self._cache_enabled:
            return
        ttl = max(0, int(getattr(self.settings, "embedding_cache_ttl_seconds", 0) or 0))
        expires_at = time.time() + ttl if ttl else 0.0
        key = self._vector_cache_key(text)
        with self._vectors_lock:
            self._vectors[key] = (expires_at, vector)
            self._vectors.move_to_end(key)
            limit = max(1, int(getattr(self.settings, "embedding_cache_max_entries", 512) or 512))
            while len(self._vectors) > limit:
                self._vectors.popitem(last=False)  # 淘汰最久未使用

    @property
    def _cache_enabled(self) -> bool:
        return bool(getattr(self.settings, "embedding_cache_enabled", True))

    def cache_info(self) -> dict:
        with self._vectors_lock:
            size = len(self._vectors)
        lookups = self._cache_hits + self._cache_misses
        return {
            "enabled": self._cache_enabled,
            "entries": size,
            "max_entries": int(getattr(self.settings, "embedding_cache_max_entries", 512) or 512),
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "hit_rate": round(self._cache_hits / lookups, 4) if lookups else 0.0,
            "requests": self._requests,
            "failures": self._failures,
            "fallbacks": self._fallbacks,
            "failure_mode": self.settings.embedding_failure_mode,
        }

    @property
    def enabled(self) -> bool:
        return (
            self.settings.embedding_provider == "openai_compatible"
            and bool(self.settings.embedding_base_url or self.settings.llm_base_url)
            and bool(self.settings.embedding_api_key or self.settings.llm_api_key)
        )

    @property
    def dim(self) -> int:
        return self.settings.embedding_dim

    @property
    def model_name(self) -> str:
        return self.settings.embedding_model if self.enabled else "stub-hashing-v1"

    def embed_query(self, text: str) -> list[float]:
        """查询向量（**走缓存**）。

        为什么只缓存查询、不缓存入库切片：一次诊断里同一段改写后的查询文本会被多个
        Agent 拿去检索不同知识库（`rewrite_query` 只留前 24 个 token，长问题的追加词会被截掉），
        所以查询向量重复率高；而切片向量每篇文档只算一次、且已经落在切片表里，
        把它们塞进缓存只会挤掉真正会复用的查询向量（LRU 容量有限）。
        """
        return self._embed_cached([text])[0] if text else []

    def embed(self, texts: list[str]) -> list[list[float]]:
        """批量向量化（入库切片用，不缓存）。

        查询向量请用 `embed_query`：那条路径才会读写向量缓存。
        """
        if not texts:
            return []
        return self._embed_uncached(texts)

    def _embed_cached(self, texts: list[str]) -> list[list[float]]:
        """按文本逐条走缓存，只把未命中的发给模型。"""
        vectors: list[list[float] | None] = [self._cache_get(text) for text in texts]
        missing = [index for index, vector in enumerate(vectors) if vector is None]
        if self._cache_enabled:
            # 统计口径与其它缓存一致：命中/未命中按"文本条数"计
            for _ in missing:
                note_lookup("embedding", False)
            for _ in range(len(texts) - len(missing)):
                note_lookup("embedding", True)
        if missing:
            computed = self._embed_uncached([texts[index] for index in missing])
            for index, vector in zip(missing, computed):
                vectors[index] = vector
                self._cache_put(texts[index], vector)
        return [vector if vector is not None else [] for vector in vectors]

    def _embed_uncached(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.enabled:
            return [self._hash_embedding(t) for t in texts]
        base = (self.settings.embedding_base_url or self.settings.llm_base_url).rstrip("/")
        key = self.settings.embedding_api_key or self.settings.llm_api_key
        self._requests += 1
        try:
            if self._client is None:
                self._client = httpx.Client(timeout=self.settings.llm_timeout_seconds)
            resp = self._client.post(
                f"{base}/embeddings",
                json={"model": self.settings.embedding_model, "input": texts},
                headers={"Authorization": f"Bearer {key}"},
            )
            resp.raise_for_status()
            data = resp.json()["data"]
            data.sort(key=lambda x: x.get("index", 0))
            vectors = [d["embedding"] for d in data]
            if len(vectors) != len(texts):
                raise ValueError("embedding result count mismatch")
            if any(
                not isinstance(vector, list)
                or len(vector) != self.dim
                or not all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    for value in vector
                )
                for vector in vectors
            ):
                raise ValueError("embedding vector shape mismatch")
            return vectors
        except Exception as exc:
            self._failures += 1
            if self.settings.embedding_failure_mode == "error":
                raise RuntimeError("Embedding 服务不可用或响应不符合契约") from exc
            self._fallbacks += 1
            logger.warning("Embedding 接口失败，回退到哈希向量: %s", exc)
            return [self._hash_embedding(t) for t in texts]

    def _hash_embedding(self, text: str) -> list[float]:
        dim = self.dim
        vec = np.zeros(dim, dtype=np.float32)
        tokens = _WORD_RE.findall(text.lower())
        cjk = _CJK_RE.findall(text)
        tokens.extend(cjk)
        tokens.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            idx = int.from_bytes(digest[:4], "big") % dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = float(np.linalg.norm(vec))
        return (vec / norm).tolist() if norm else vec.tolist()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


_gateway: ModelGateway | None = None
_embedder: EmbeddingClient | None = None


def get_model_gateway() -> ModelGateway:
    global _gateway
    if _gateway is None:
        _gateway = ModelGateway()
    return _gateway


def get_embedding_client() -> EmbeddingClient:
    global _embedder
    if _embedder is None:
        _embedder = EmbeddingClient()
    return _embedder


def mask_secret(value: str) -> str:
    if not value:
        return ""
    return f"{value[:4]}***{value[-2:]}" if len(value) > 8 else "***"


def sanitize_for_log(payload: Any) -> Any:
    """日志脱敏：密钥、口令、token 一律打码。"""
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            lk = str(k).lower()
            if any(s in lk for s in ("key", "secret", "password", "token", "authorization")):
                out[k] = mask_secret(str(v))
            else:
                out[k] = sanitize_for_log(v)
        return out
    if isinstance(payload, list):
        return [sanitize_for_log(v) for v in payload]
    return payload
