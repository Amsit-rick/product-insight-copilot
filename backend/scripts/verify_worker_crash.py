"""多副本验收（第二部分）：worker 崩溃后任务能不能被另一个 worker 接管。

**为什么必须真杀进程**：租约回收（lease reclaim）在单元测试里是"手工把 `lease_expires_at`
改到过去"，那验证的是 SQL 条件，不是"进程真的没了之后系统会不会自愈"。

**杀谁**：从 `task_queue_items.claimed_by` 取当前持有者（worker 标识就是 `主机名-pid`）。
宿主机进程模式用 `taskkill /T /F`；Compose 模式根据主机名定位容器并执行 `docker rm -f`。
两种模式都不是随便挑一个 worker，而是**精确杀掉握着这条队列项的持有者**。

跑之前**至少要有 2 个 worker**（要杀 1 个，得还剩得下 1 个来接）：

    powershell -ExecutionPolicy Bypass -File .tmp\\run_worker.ps1    # 开两个

用法：

    ..\\.venv\\Scripts\\python.exe scripts\\verify_worker_crash.py

环境变量同 `verify_multiprocess.py`（`MULTI_API` / `MULTI_PG_URL` / `MULTI_REDIS`）。

Docker Compose 模式额外设置：

    set MULTI_WORKER_KILL_MODE=docker
    set MULTI_COMPOSE_PROJECT=pic-ticket-audit
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

API = os.environ.get("MULTI_API", "http://127.0.0.1:8020")
PG_URL = os.environ.get("MULTI_PG_URL", "postgresql+psycopg://pic:picpass@127.0.0.1:55432/picdb")
REDIS_URL = os.environ.get("MULTI_REDIS", "redis://127.0.0.1:56379/0")
KILL_MODE = os.environ.get("MULTI_WORKER_KILL_MODE", "host").strip().lower()
COMPOSE_PROJECT = os.environ.get("MULTI_COMPOSE_PROJECT", "").strip()

os.environ["DATABASE_URL"] = PG_URL

import httpx  # noqa: E402
import redis  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

QUESTION = "Android 端 3.2.0 发布后次日留存下降，哪些用户受到影响？版本变更与故障记录是否相关？"
FILTERS = {
    "product": "星野社区",
    "platform": "Android",
    "app_version": "3.2.0",
    "date_from": "2026-08-01",
    "date_to": "2026-08-31",
    "knowledge_bases": ["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
    "metrics": ["next_day_retention", "crash_rate"],
    "priority": "P1",
}

RESULTS: list[tuple[str, bool, str]] = []
lines: list[str] = []


def log(message: str = "") -> None:
    lines.append(message)
    print(message)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    log(f"  [{'PASS' if ok else 'FAIL'}] {name}{' — ' + detail if detail else ''}")


def _run_command(args: list[str], *, encoding: str = "utf-8") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding=encoding,
        errors="replace",
    )


def _docker_workers() -> list[str]:
    if not COMPOSE_PROJECT:
        return []
    result = _run_command(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={COMPOSE_PROJECT}",
            "--filter",
            "label=com.docker.compose.service=worker",
            "--format",
            "{{.ID}}",
        ]
    )
    if result.returncode != 0:
        return []
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def _alive_workers() -> list[str]:
    """当前在跑的 worker 进程 PID（命令行含 app.worker）。

    用 PowerShell 查而不是 wmic：新版本 Windows 上 wmic 已被移除，用它只会拿到 FileNotFoundError。
    """
    if KILL_MODE == "docker":
        return _docker_workers()
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -match 'app.worker' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    output = _run_command(["powershell", "-NoProfile", "-Command", script]).stdout
    return sorted({line.strip() for line in output.splitlines() if line.strip().isdigit()})


def _kill_worker(worker_id: str) -> tuple[bool, str]:
    if KILL_MODE == "docker":
        hostname = worker_id.rsplit("-", 1)[0]
        matches = [container_id for container_id in _docker_workers() if container_id.startswith(hostname)]
        if len(matches) != 1:
            return False, f"无法唯一定位 worker 容器：hostname={hostname} matches={matches}"
        result = _run_command(["docker", "rm", "-f", matches[0]])
        detail = (result.stdout or result.stderr or "").strip()[:120]
        return result.returncode == 0, detail

    victim_pid = worker_id.rsplit("-", 1)[-1]
    result = _run_command(["taskkill", "/PID", victim_pid, "/T", "/F"], encoding="gbk")
    detail = (result.stdout or result.stderr or "").strip()[:120]
    return result.returncode == 0, detail


def main() -> int:
    engine = create_engine(PG_URL, future=True)
    cache = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    stale = cache.keys("pic:rate:*")
    if stale:
        cache.delete(*stale)

    with httpx.Client(base_url=API, timeout=60.0) as client:
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        log("=== 准备：确认至少有 2 个 worker 在跑（本次要杀 1 个，必须还剩得下 1 个来接）===")
        alive = _alive_workers()
        check("运行前至少有 2 个 worker 进程", len(alive) >= 2, f"当前 {len(alive)} 个：{alive}")
        if len(alive) < 2:
            log("  [中止] worker 不足，无法验证「被杀之后有人接管」。请先起 2 个 worker。")
            return 1

        log("\n=== 准备：提交 3 个任务，让 worker 忙起来 ===")
        task_ids: list[str] = []
        for index in range(3):
            resp = client.post("/api/v1/tasks", headers=headers,
                               json={"query": f"{QUESTION}（崩溃回收第 {index} 号）", **FILTERS})
            if resp.status_code == 202:
                task_ids.append(resp.json()["id"])
        check("提交 3 个任务", len(task_ids) == 3, f"{len(task_ids)} 个")

        log("\n=== 捕捉一个「正在被某个 worker 持有」的队列项 ===")
        victim = None
        deadline = time.time() + 30
        while time.time() < deadline and victim is None:
            with engine.connect() as conn:
                victim = conn.execute(
                    text(
                        "SELECT id, task_id, claimed_by, attempts FROM task_queue_items "
                        "WHERE status = 'claimed' AND task_id = ANY(:ids) "
                        "ORDER BY claimed_at LIMIT 1"
                    ),
                    {"ids": task_ids},
                ).first()
            if victim is None:
                time.sleep(0.2)
        check("抓到了处于 claimed 状态的队列项", victim is not None,
              f"item={victim[0][:8]}… claimed_by={victim[2]} attempts={victim[3]}" if victim else "未抓到")
        if victim is None:
            return 1

        victim_worker = victim[2]
        victim_task = victim[1]

        log(f"\n=== 精确杀掉持有者：worker={victim_worker} mode={KILL_MODE} ===")
        killed, kill_detail = _kill_worker(str(victim_worker))
        check("持有者已被终止（进程/容器真的没了）", killed, kill_detail)
        time.sleep(1.0)

        log("\n=== 等待租约过期 → 另一个 worker 应该接管 ===")
        reclaimed = None
        deadline = time.time() + 90
        while time.time() < deadline:
            with engine.connect() as conn:
                reclaimed = conn.execute(
                    text("SELECT status, attempts, claimed_by, last_error FROM task_queue_items WHERE id = :i"),
                    {"i": victim[0]},
                ).first()
            if reclaimed and reclaimed[0] == "done":
                break
            time.sleep(1.0)

        check("被中断的队列项最终完成（自愈成功）",
              reclaimed is not None and reclaimed[0] == "done",
              f"status={reclaimed[0]} attempts={reclaimed[1]} claimed_by={reclaimed[2]}")
        check("attempts 从 1 变成 2（重新领取后跑的）",
              reclaimed is not None and reclaimed[1] >= 2, f"attempts={reclaimed[1] if reclaimed else '-'}")
        check("由**另一个** worker 进程接管",
              reclaimed is not None and reclaimed[2] != victim_worker,
              f"原持有者={victim_worker} → 接管者={reclaimed[2] if reclaimed else '-'}")
        if reclaimed and reclaimed[3]:
            log(f"      last_error（保留了中断痕迹）：{str(reclaimed[3])[:110]}")

        final = client.get(f"/api/v1/tasks/{victim_task}", headers=headers).json()["status"]
        check("被中断的任务本身也跑到了终态", final in {"completed", "waiting_human"}, f"状态={final}")

        with engine.connect() as conn:
            agent_rows = conn.execute(
                text("SELECT agent_code, count(*) FROM agent_runs WHERE task_id = :t GROUP BY agent_code"),
                {"t": victim_task},
            ).fetchall()
            start_rows = conn.execute(
                text(
                    "SELECT node, count(*) FROM analysis_events WHERE task_id = :t "
                    "AND node IS NOT NULL AND message LIKE '%开始执行%' GROUP BY node"
                ),
                {"t": victim_task},
            ).fetchall()
        total_runs = sum(count for _, count in agent_rows)
        max_starts = max((count for _, count in start_rows), default=0)
        log("\n  崩溃后到底重做了多少？——用数据说话，别靠猜：")
        log(f"      agent_runs 记录数 = {total_runs}（4 个分析 Agent 各 1 次 = 没有被重做）")
        log(f"      单节点最多「开始执行」次数 = {max_starts}")
        if reclaimed is not None and reclaimed[0] == "done":
            attempts = int(reclaimed[1])
            check("重做范围有界（每个节点开始次数 ≤ 领取次数）", max_starts <= attempts,
                  f"最多开始 {max_starts} 次 / 领取 {attempts} 次")
        else:
            log("      [跳过] 队列项未完成，样本不足以下结论（不做「看起来通过」的断言）")
        log("  → 如实结论：队列保证「同一时刻只有一个 worker 持有该任务」（不重复领取）；")
        log("    崩溃后被回收的任务由另一个 worker 接着处理，但**已完成的节点是否重做取决于")
        log("    检查点的提交时机**（实测两次：一次全部续跑，一次 4 个分析 Agent 全部重做）。")
        log("    这是 at-least-once 的固有代价，不能宣传成 exactly-once。")

        log("\n=== 其余任务不受影响 ===")
        others = [tid for tid in task_ids if tid != victim_task]
        finals = [client.get(f"/api/v1/tasks/{tid}", headers=headers).json()["status"] for tid in others]
        check("另外两个任务正常跑到终态",
              all(item in {"completed", "waiting_human"} for item in finals), json.dumps(finals))

    failed = [item for item in RESULTS if not item[1]]
    log(f"\n=== 汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    if failed:
        log("失败项：" + "、".join(item[0] for item in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
