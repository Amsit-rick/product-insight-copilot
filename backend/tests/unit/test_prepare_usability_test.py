"""客服无指导测试准备脚本的脱敏输出契约。"""

from __future__ import annotations

import importlib.util
import json
import sys
from types import SimpleNamespace
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "prepare_usability_test.py"
SPEC = importlib.util.spec_from_file_location("prepare_usability_test", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def test_preparation_payload_is_safe_and_complete():
    tickets = {
        "TK-260924-0186": SimpleNamespace(
            last_analysis_task_id="analysis-1",
            subject="演示客户的物流问题",
        ),
        "TK-260924-0184": SimpleNamespace(last_analysis_task_id=None),
    }

    payload = module.build_test_payload("demo", tickets)

    assert payload["schema_version"] == "after-sales-usability-prep-v1"
    assert payload["not_a_user_test_result"] is True
    assert {item["code"] for item in payload["tasks"]} == {"A", "B", "C", "D", "E"}
    task_a = next(item for item in payload["tasks"] if item["code"] == "A")
    task_c = next(item for item in payload["tasks"] if item["code"] == "C")
    task_e = next(item for item in payload["tasks"] if item["code"] == "E")
    assert task_a["ticket_available"] is True
    assert task_a["ticket_analyzed"] is True
    assert task_c["ticket_available"] is False
    assert task_e["ticket_id"] is None
    assert task_e["ticket_available"] is None

    encoded = json.dumps(payload, ensure_ascii=False)
    assert "ops123" not in encoded
    assert "reviewer123" not in encoded
    assert "演示客户的物流问题" not in encoded
    assert "完成率" not in encoded


def test_preparation_payload_can_use_a_non_default_port():
    payload = module.build_test_payload("demo", {}, base_url="http://127.0.0.1:8000")

    assert payload["test_url"] == "http://127.0.0.1:8000/#/login"


def test_default_preparation_url_matches_local_dev_entrypoint():
    payload = module.build_test_payload("demo", {})

    assert payload["test_url"] == "http://127.0.0.1:8000/#/login"
