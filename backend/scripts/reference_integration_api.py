#!/usr/bin/env python
"""Start the independent local integration sandbox."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import uvicorn  # noqa: E402

from sandbox_services.app import create_app  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="本地集成 Sandbox：事实、指标和 Webhook 接口",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args(argv)

    print(f"reference integration sandbox: http://{args.host}:{args.port}")
    print(f"OpenAPI: http://{args.host}:{args.port}/docs")
    print("仅限本地/隔离环境；密钥从 SANDBOX_API_KEY 和 SANDBOX_WEBHOOK_SECRET 读取。")
    uvicorn.run(
        create_app(),
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
