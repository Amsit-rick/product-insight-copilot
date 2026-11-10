"""Inspect shared storage configuration or run an explicitly authorized probe."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def _write_json(path: str | None, payload: dict) -> None:
    if not path:
        return
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"JSON 证据={output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="共享对象存储安全验收")
    parser.add_argument(
        "--allow-real-storage-calls",
        action="store_true",
        help="显式授权对配置的测试桶执行最小 put/get/delete",
    )
    parser.add_argument(
        "--action",
        choices=("full", "write", "read", "delete"),
        default="full",
    )
    parser.add_argument("--probe-id", default="storage-probe-default")
    parser.add_argument("--json-out", help="输出不含端点、密钥、对象键和正文的 JSON 证据")
    args = parser.parse_args(argv)

    from app.config import get_settings
    from app.services.object_storage_probe import run_object_storage_probe

    result = run_object_storage_probe(
        get_settings(),
        allow_real_calls=args.allow_real_storage_calls,
        action=args.action,
        probe_id=args.probe_id,
    )
    print("=== 共享对象存储验收 ===")
    print("配置=" + ("READY" if result["configuration"]["passed"] else "NOT_READY"))
    if result["executed"]:
        print("动作=" + result["action"])
        print("结果=" + ("PASS" if result["passed"] else "FAIL"))
    else:
        print("真实存储调用=SKIP (" + result["reason"] + ")")
    print("shared_storage_ready=" + str(result["shared_storage_ready"]).lower())
    print("production_ready=" + str(result["production_ready"]).lower())
    _write_json(args.json_out, result)

    if not args.allow_real_storage_calls:
        return 0
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
