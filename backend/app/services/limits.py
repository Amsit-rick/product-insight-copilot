"""限流与并发配额（设计文档 §15 阶段六：稳定性）。

实现方式：基于 `Cache`（Redis / SQLite / 进程内）的**固定窗口原子计数器**。

为什么选固定窗口而不是令牌桶：
- 目标是"防止脚本误刷把成本烧掉"，对边界平滑度要求低；
- 固定窗口只需要一次原子递增，各缓存后端行为一致，好测试；
- 代价是窗口交界处可能瞬时放行 2 倍流量——对"每分钟 N 次建任务"这种场景可以接受，
  真要平滑再换令牌桶（接口不变）。

注意：这是**软限流**，多副本部署时各副本共享 Redis 计数（因此是准确的）；
内存后端只在本进程内计数。
"""

from __future__ import annotations

import time

from app.logging_utils import get_logger
from app.services.cache import get_cache

logger = get_logger(__name__)


class RateLimitUnavailable(RuntimeError):
    """无法可靠执行安全限流；调用方应暂时拒绝请求。"""


class RateLimiter:
    def __init__(self) -> None:
        self.cache = get_cache()

    def check(
        self, key: str, *, limit: int, window_seconds: int = 60,
        fail_closed: bool = False, require_shared: bool = False,
    ) -> tuple[bool, int]:
        """返回 (是否允许, 剩余等待秒数)。

        `limit <= 0` 表示不限流。登录使用 fail_closed；生产登录还要求共享 Redis。
        """
        if require_shared and self.cache.backend_name != "redis":
            raise RateLimitUnavailable("共享限流后端不可用")
        if limit <= 0:
            return True, 0

        now = int(time.time())
        bucket = now // window_seconds
        cache_key = f"pic:rate:{key}:{bucket}"
        try:
            current = self.cache.increment(cache_key, ttl=window_seconds * 2)
            if current > limit:
                return False, window_seconds - (now % window_seconds)
            return True, 0
        except Exception as exc:
            # 异常正文可能包含带凭据的连接串，仅记录类型。
            if fail_closed or require_shared:
                logger.warning("安全限流不可用，拒绝请求: %s", type(exc).__name__)
                raise RateLimitUnavailable("限流计数暂不可用") from None
            logger.warning("限流检查失败，放行本次请求: %s", type(exc).__name__)
            return True, 0

    def used(self, key: str, *, window_seconds: int = 60) -> int:
        bucket = int(time.time()) // window_seconds
        try:
            return int(self.cache.get(f"pic:rate:{key}:{bucket}") or 0)
        except Exception:  # pragma: no cover
            return 0

    def reset(self, key: str, *, window_seconds: int = 60) -> None:
        bucket = int(time.time()) // window_seconds
        try:
            self.cache.delete(f"pic:rate:{key}:{bucket}")
        except Exception:  # pragma: no cover
            pass


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter


def reset_rate_limiter() -> None:
    global _limiter
    _limiter = None
