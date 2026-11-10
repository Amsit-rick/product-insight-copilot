"""日志工具。

设计文档 11 节：禁止把模型密钥、完整敏感内容写入普通日志。
这里统一提供脱敏后的结构化日志输出。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict) and extra:
            base += " | " + json.dumps(extra, ensure_ascii=False, default=str)
        return base


def configure_logging() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    settings = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.app_env in {"prod", "dev"} else TextFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG if settings.debug else logging.INFO)
    # httpx/httpcore 的 INFO/DEBUG 记录会包含完整请求 URL。模型、事实接口和
    # Webhook 端点可能带租户路径或不可公开的路由，因此只保留告警级别。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def log_event(logger: logging.Logger, level: int, message: str, **fields: Any) -> None:
    """带附加字段的日志输出，附加字段统一走脱敏。"""
    from app.services.model_gateway import sanitize_for_log

    logger.log(level, message, extra={"extra_fields": sanitize_for_log(fields)})
