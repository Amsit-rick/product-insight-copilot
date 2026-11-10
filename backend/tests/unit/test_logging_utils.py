from __future__ import annotations

import logging
from types import SimpleNamespace

from app import logging_utils


def test_http_client_loggers_do_not_emit_configured_endpoint_urls(monkeypatch):
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    original_configured = logging_utils._CONFIGURED
    original_httpx_level = logging.getLogger("httpx").level
    original_httpcore_level = logging.getLogger("httpcore").level
    try:
        logging_utils._CONFIGURED = False
        logging.getLogger("httpx").setLevel(logging.NOTSET)
        logging.getLogger("httpcore").setLevel(logging.NOTSET)
        monkeypatch.setattr(
            logging_utils,
            "get_settings",
            lambda: SimpleNamespace(app_env="dev", debug=True),
        )

        logging_utils.configure_logging()

        assert logging.getLogger("httpx").level >= logging.WARNING
        assert logging.getLogger("httpcore").level >= logging.WARNING
    finally:
        root.handlers = original_handlers
        root.setLevel(original_level)
        logging.getLogger("httpx").setLevel(original_httpx_level)
        logging.getLogger("httpcore").setLevel(original_httpcore_level)
        logging_utils._CONFIGURED = original_configured
