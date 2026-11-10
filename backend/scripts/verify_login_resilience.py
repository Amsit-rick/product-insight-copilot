"""真实 Redis 与 Uvicorn 网络栈下的登录限流故障/恢复验收。"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path


def request_json(url: str, *, payload: dict | None = None, headers: dict | None = None) -> tuple[int, dict, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read().decode("utf-8")
            return response.status, json.loads(raw), dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw), dict(exc.headers)


def docker(action: str, container: str) -> None:
    subprocess.run(["docker", action, container], check=True, capture_output=True, text=True)


def header_value(headers: dict, name: str) -> str | None:
    expected = name.lower()
    return next((str(value) for key, value in headers.items() if key.lower() == expected), None)


def wait_login(base_url: str, username: str, password: str, *, timeout: float = 20) -> tuple[int, dict, dict]:
    deadline = time.time() + timeout
    last: tuple[int, dict, dict] | None = None
    while time.time() < deadline:
        last = request_json(
            f"{base_url}/api/v1/auth/login",
            payload={"username": username, "password": password},
        )
        if last[0] == 200:
            return last
        time.sleep(0.5)
    raise RuntimeError(f"Redis 恢复后登录未恢复，最后状态={last[0] if last else 'none'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="验证登录限流的代理、防故障与恢复行为")
    parser.add_argument("--base-url", default="http://127.0.0.1:18300")
    parser.add_argument("--redis-container", required=True)
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password", default="admin123")
    parser.add_argument("--limit", type=int, default=2)
    parser.add_argument("--json-out", default="", help="可选：写入脱敏 JSON 证据")
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    status, health, _ = request_json(f"{base_url}/health")
    if status != 200 or health.get("env") != "prod":
        raise RuntimeError("目标必须是 APP_ENV=prod 的健康 API")
    if health.get("cache", {}).get("backend") != "redis":
        raise RuntimeError("演练开始前必须实际使用 Redis")
    # 上一次故障可能留下 runtime_error；用一次真实计数验证恢复并清除运行时错误状态。
    wait_login(base_url, args.username, args.password)
    _, health, _ = request_json(f"{base_url}/health")
    if not health.get("cache", {}).get("healthy"):
        raise RuntimeError("演练开始前 Redis 未恢复健康")

    probe_user = f"rate-probe-{int(time.time())}"
    proxy_statuses = []
    for index in range(args.limit + 1):
        code, _, _ = request_json(
            f"{base_url}/api/v1/auth/login",
            payload={"username": probe_user, "password": "wrong-password"},
            headers={"X-Forwarded-For": f"198.51.100.{index + 1}"},
        )
        proxy_statuses.append(code)
    expected = [401] * args.limit + [429]
    if proxy_statuses != expected:
        raise RuntimeError(f"转发头可能改变限流身份：expected={expected}, actual={proxy_statuses}")

    paused = False
    try:
        docker("pause", args.redis_container)
        paused = True
        code, body, headers = request_json(
            f"{base_url}/api/v1/auth/login",
            payload={"username": args.username, "password": args.password},
        )
        if code != 503 or body.get("detail", {}).get("code") != "login_temporarily_unavailable":
            raise RuntimeError(f"Redis 故障时未安全拒绝登录：status={code}, body={body}")
        if header_value(headers, "Retry-After") != "30":
            raise RuntimeError("Redis 故障响应缺少 Retry-After: 30")
        _, outage_health, _ = request_json(f"{base_url}/health")
        cache_health = outage_health.get("cache", {})
        if cache_health.get("healthy") is not False or not cache_health.get("runtime_error"):
            raise RuntimeError(f"健康接口未反映运行时 Redis 故障：{cache_health}")
    finally:
        if paused:
            docker("unpause", args.redis_container)

    recovered_code, _, _ = wait_login(base_url, args.username, args.password)
    _, recovered_health, _ = request_json(f"{base_url}/health")
    recovered_cache = recovered_health.get("cache", {})
    if recovered_cache.get("healthy") is not True or recovered_cache.get("runtime_error") is not None:
        raise RuntimeError(f"Redis 恢复后健康状态未恢复：{recovered_cache}")

    result = {
        "proxy_header_statuses": proxy_statuses,
        "redis_outage_login_status": 503,
        "redis_recovery_login_status": recovered_code,
        "cache_healthy_after_recovery": True,
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_out:
        output = Path(args.json_out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
