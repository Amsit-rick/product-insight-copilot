"""Run an explicitly authorized, redacted target-environment readiness probe."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def _parse_node(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("节点格式必须为 LABEL=BASE_URL")
    label, url = value.split("=", 1)
    return label.strip(), url.strip()


def _write_json(path: str | None, payload: dict) -> None:
    if not path:
        return
    output = Path(path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"JSON 证据={output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="目标环境与跨主机部署验收")
    parser.add_argument("--node", action="append", type=_parse_node, default=[], help="LABEL=BASE_URL，可重复")
    parser.add_argument("--expected-env", default="prod", choices=("dev", "test", "prod"))
    parser.add_argument("--expected-release-sha", default="")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--allow-real-target-calls", action="store_true", help="显式授权访问目标 /health/ready")
    parser.add_argument("--allow-insecure-http", action="store_true", help="仅限本地/CI，允许 HTTP")
    parser.add_argument(
        "--confirm-distinct-hosts",
        action="store_true",
        help="操作者确认各节点运行在不同主机；URL 本身不能自动证明该事实",
    )
    parser.add_argument("--json-out", help="输出不含原始 URL、响应正文或凭据的 JSON 证据")
    args = parser.parse_args(argv)

    from app.services.target_environment_probe import run_target_environment_probe

    result = run_target_environment_probe(
        args.node,
        expected_env=args.expected_env,
        allow_real_calls=args.allow_real_target_calls,
        allow_insecure_http=args.allow_insecure_http,
        confirm_distinct_hosts=args.confirm_distinct_hosts,
        expected_release_sha=args.expected_release_sha,
        timeout_seconds=args.timeout_seconds,
    )
    print("=== 目标环境验收 ===")
    print("配置=" + ("READY" if result["configuration"]["passed"] else "NOT_READY"))
    if result["executed"]:
        print("运行形态=" + ("PASS" if result["runtime_shape_ready"] else "FAIL"))
        print("目标生产环境=" + ("PASS" if result["target_environment_ready"] else "NOT_PROVEN"))
        print(
            "跨主机证据="
            + ("PASS" if result["cross_host_or_target_environment_verified"] else "NOT_PROVEN")
        )
    else:
        print("真实目标调用=SKIP (" + result["reason"] + ")")
    print("production_ready=" + str(result["production_ready"]).lower())
    _write_json(args.json_out, result)

    if not args.allow_real_target_calls:
        return 0
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
