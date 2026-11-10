"""演示政策库的覆盖检查：促销问题是真实评测里暴露过的内容缺口。"""

from __future__ import annotations

import json
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
DEMO = BACKEND / "data" / "demo"


def test_promotion_policy_is_present_and_current():
    manifest = json.loads((DEMO / "manifest.json").read_text(encoding="utf-8"))
    policies = manifest.get("policies") or []
    promo = [
        item
        for item in policies
        if (item.get("policy") or {}).get("title") == "促销优惠与满减规则"
    ]
    assert promo, "演示政策库缺少促销/满减政策，promotion 评测会因无依据被判失败"

    item = promo[0]
    policy = item["policy"]
    assert item["kb"] == "policy_kb"
    assert policy["version"] == "v3.0"
    assert policy["effective_from"] == "2026-07-01"
    assert policy["effective_to"] is None
    assert (DEMO / item["path"]).exists(), "政策清单指向的文件不存在"

    content = (DEMO / item["path"]).read_text(encoding="utf-8")
    assert "满减" in content
    assert "不自动执行退款" in content, "促销政策必须明确不自动执行写操作"
