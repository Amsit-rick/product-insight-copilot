"""适配器测试文件（空占位，见同目录下的 test_order_adapters.py）。

保留该文件只是为了让 `app.adapters` 成为明确的包；测试用例在 tests/unit 下。
"""

from app.adapters.base import AdapterResult, FactAdapter
from app.adapters.http import HttpFactAdapter
from app.adapters.mcp import McpFactAdapter
from app.adapters.mock import MockFactAdapter, demo_order_ids
from app.adapters.registry import get_fact_adapter, reset_adapter_cache

__all__ = [
    "AdapterResult",
    "FactAdapter",
    "HttpFactAdapter",
    "McpFactAdapter",
    "MockFactAdapter",
    "demo_order_ids",
    "get_fact_adapter",
    "reset_adapter_cache",
]
