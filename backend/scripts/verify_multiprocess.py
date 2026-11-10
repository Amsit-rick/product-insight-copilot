"""多副本验收：真 Postgres + 真 Redis + 独立 worker 进程（可复现）。

这个脚本回答的是**单元/集成测试回答不了**的问题：那些测试全部跑在"单进程 + SQLite + 内存缓存"里，
而生产形态是"API 进程 + N 个 worker 进程 + Postgres + Redis"。两者之间最容易出事的差别：

1. 接口只入队、由**独立 worker 进程**领取执行（不再是进程内 threadpool）；
2. 并发提交下**同一条队列项不会被重复执行**（重复领取会表现为节点被跑两遍）；
3. **Redis 真的在用**，缓存能**跨进程**读到（降级到落盘/内存时做不到）；
4. 提交限流是**全局**计数（键在 Redis，多副本共享同一计数）；
5. 人工审核恢复走**共享断点**，由另一个 worker 进程继续执行。

前置条件（本机 Docker 里有 postgres / redis 镜像即可，不需要联网）：

    docker run -d --name pic-pg      -e POSTGRES_PASSWORD=picpass -e POSTGRES_USER=pic ^
        -e POSTGRES_DB=picdb -p 55432:5432 postgres:16-alpine
    docker run -d --name pic-redis   -p 56379:6379 redis:7-alpine

    # 1) 建表（走真实迁移，不用 create_all）
    set DATABASE_URL=postgresql+psycopg://pic:picpass@127.0.0.1:55432/picdb
    ..\\.venv\\Scripts\\python.exe -m app.db.migrate apply        # 无 pgvector 扩展时 002 会失败并整体回滚
    ..\\.venv\\Scripts\\python.exe -m app.db.seed                 # 或按 §7.4.1 的说明只应用 001

    # 2) 起 API 与两个 worker（环境变量见 .tmp/multiprocess_env.ps1 示例）
    # 3) 跑本脚本
    ..\\.venv\\Scripts\\python.exe scripts\\verify_multiprocess.py

可用的环境变量（都有默认值）：
    MULTI_API      被测服务地址，默认 http://127.0.0.1:8020
    MULTI_PG_URL   Postgres 连接串（用于直接查队列状态做证据）
    MULTI_REDIS    Redis 连接串（用于确认缓存/限流真的落在 Redis）
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

API = os.environ.get("MULTI_API", "http://127.0.0.1:8020")
PG_URL = os.environ.get("MULTI_PG_URL", "postgresql+psycopg://pic:picpass@127.0.0.1:55432/picdb")
REDIS_URL = os.environ.get("MULTI_REDIS", "redis://127.0.0.1:56379/0")

# 必须在导入任何 app 模块之前设好：Settings 是 lru_cache 的，import 之后再改环境变量不生效
# （第一版就是这么踩的：另一个进程读缓存时用的还是旧配置，降级成了 sqlite）
os.environ["DATABASE_URL"] = PG_URL
os.environ["CACHE_BACKEND"] = "redis"
os.environ["REDIS_URL"] = REDIS_URL

import httpx  # noqa: E402
import redis  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

QUESTION = (
    "Android 端 3.2.0 发布后次日留存下降，哪些用户受到影响？"
    "版本变更、用户反馈和故障记录是否支持版本相关性？下一步应该优先验证什么？"
)
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


def login(client: httpx.Client, username: str, password: str) -> dict:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    resp.raise_for_status()
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def wait_terminal(client: httpx.Client, headers: dict, task_id: str, timeout: float = 240) -> str:
    deadline = time.time() + timeout
    status = "pending"
    while time.time() < deadline:
        status = client.get(f"/api/v1/tasks/{task_id}", headers=headers).json()["status"]
        if status in {"completed", "failed", "cancelled", "waiting_human"}:
            return status
        time.sleep(0.5)
    return status


def node_visits(conn, task_ids: list[str]) -> dict[str, dict]:
    """取每个任务最终事件里的 `node_visits`：它记录"每个节点被访问了几次"，是重复执行的直接证据。

    注意**不能**按 (task_id, node) 数事件条数：每个节点本来就有"开始/完成"两条事件，
    那样数出来永远是 2（第一版就是这么误报的）。
    """
    rows = conn.execute(
        text(
            "SELECT task_id, payload FROM analysis_events "
            "WHERE task_id = ANY(:ids) AND node = 'runner' ORDER BY created_at"
        ),
        {"ids": task_ids},
    ).fetchall()
    result: dict[str, dict] = {}
    for task_id, payload in rows:
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, dict) and payload.get("node_visits"):
            result[task_id] = payload["node_visits"]
    return result


def main() -> int:
    engine = create_engine(PG_URL, future=True)
    cache = redis.Redis.from_url(REDIS_URL, decode_responses=True)

    stale = cache.keys("pic:rate:*")
    if stale:
        cache.delete(*stale)
    log(f"（重置限流窗口：清了 {len(stale)} 个键）")

    with httpx.Client(base_url=API, timeout=60.0) as client:
        health = client.get("/health").json()
        log("\n=== 0. 运行形态自检 ===")
        check("数据库是 Postgres", health["database"] == "postgres", health["database"])
        check("缓存后端是 Redis 且健康",
              health["cache"]["backend"] == "redis" and health["cache"]["healthy"],
              f"{health['cache']['backend']} healthy={health['cache']['healthy']}")
        check("执行后端是 queue 且 API 进程内不跑 worker",
              health["execution"]["backend"] == "queue" and health["execution"]["worker_embedded"] is False,
              f"{health['execution']['backend']} embedded={health['execution']['worker_embedded']}")
        check("断点恢复跨进程共享", health["execution"]["checkpointer"]["shared"] is True,
              health["execution"]["checkpointer"]["backend"])
        log(f"      （向量后端 = {health['vector_backend']}；无 pgvector 扩展时应为 fallback）")

        pm = login(client, "pm", "pm123")
        admin = login(client, "admin", "admin123")
        reviewer = login(client, "reviewer", "reviewer123")

        log("\n=== 1. 接口只入队，由独立 worker 进程执行 ===")
        created = client.post("/api/v1/tasks", headers=admin, json={"query": QUESTION, **FILTERS})
        check("建任务返回 202", created.status_code == 202, str(created.status_code))
        task_id = created.json()["id"]
        immediate = client.get(f"/api/v1/tasks/{task_id}", headers=admin).json()["status"]
        queue_now = client.get("/health").json()["execution"]["queue"]
        check("接口返回时任务还没跑（队列形态，不是同步执行）", immediate in {"pending", "running"},
              f"立即状态={immediate} 队列={json.dumps(queue_now, ensure_ascii=False)}")
        status = wait_terminal(client, admin, task_id)
        check("独立 worker 把任务跑完", status in {"completed", "waiting_human"}, f"最终状态={status}")

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT status, attempts, claimed_by FROM task_queue_items "
                     "WHERE task_id = :t ORDER BY created_at LIMIT 1"),
                {"t": task_id},
            ).first()
        check("队列项记录了 worker 领取者", row is not None and bool(row[2]),
              f"status={row[0]} attempts={row[1]} claimed_by={row[2]}")
        check("只领取执行一次（attempts=1）", row is not None and row[1] == 1, f"attempts={row[1]}")

        log("\n=== 2. 并发提交：同一条队列项不会被重复执行 ===")
        batch: list[str] = []
        for index in range(4):
            resp = client.post("/api/v1/tasks", headers=pm,
                               json={"query": f"{QUESTION}（并发第 {index} 号）", **FILTERS})
            if resp.status_code == 202:
                batch.append(resp.json()["id"])
        check("并发提交 4 个任务都被接受", len(batch) == 4, f"接受 {len(batch)} 个")
        finals = [wait_terminal(client, pm, tid) for tid in batch]
        check("4 个任务全部跑到终态",
              all(item in {"completed", "waiting_human"} for item in finals), json.dumps(finals))

        all_ids = batch + [task_id]
        with engine.connect() as conn:
            visits = node_visits(conn, all_ids)
            agents = conn.execute(
                text("SELECT task_id, agent_code, count(*) FROM agent_runs "
                     "WHERE task_id = ANY(:ids) GROUP BY task_id, agent_code HAVING count(*) > 1"),
                {"ids": all_ids},
            ).fetchall()
            queue_stats = conn.execute(
                text("SELECT status, count(*) FROM task_queue_items WHERE task_id = ANY(:ids) GROUP BY status"),
                {"ids": all_ids},
            ).fetchall()
            live_claims = conn.execute(
                text("SELECT task_id, count(*) FROM task_queue_items "
                     "WHERE task_id = ANY(:ids) AND status = 'claimed' GROUP BY task_id HAVING count(*) > 1"),
                {"ids": all_ids},
            ).fetchall()
        repeated = {tid: {node: n for node, n in value.items() if n > 1} for tid, value in visits.items()}
        repeated = {tid: detail for tid, detail in repeated.items() if detail}
        check("每个任务的 node_visits 全是 1（没有任何节点被重复执行）", not repeated,
              f"异常={repeated if repeated else '无'}")
        check("同一 Agent 没有产生两次运行记录", not agents, f"{agents if agents else '无'}")
        check("同一任务不会同时存在两条 claimed 队列项", not live_claims,
              f"{live_claims if live_claims else '无'}")
        check("队列项最终都是 done、无死信", all(item[0] == "done" for item in queue_stats),
              json.dumps({item[0]: item[1] for item in queue_stats}, ensure_ascii=False))

        log("\n=== 3. Redis 真的在用，且缓存可跨进程读 ===")
        qa = client.post("/api/v1/qa/ask", headers=pm,
                         json={"query": "3.2.0 版本发布后次日留存下降，版本变更有哪些相关记录？",
                               "top_k": 5, "use_model": False})
        check("问答接口正常返回", qa.status_code == 200, str(qa.status_code))
        keys = cache.keys("pic:*")
        retrieval_keys = sorted(k for k in keys if k.startswith("pic:retrieval"))
        check("缓存条目确实写在 Redis 里", cache.dbsize() > 0 and len(retrieval_keys) > 0,
              f"dbsize={cache.dbsize()} retrieval 键={len(retrieval_keys)}")

        from app.config import get_settings  # noqa: PLC0415
        from app.services.cache import Cache  # noqa: PLC0415

        get_settings.cache_clear()
        fresh = Cache()
        payload = fresh.get(retrieval_keys[0])
        check("另一个进程能读到 API 进程写的缓存（真跨进程共享）",
              fresh.backend_name == "redis" and payload is not None,
              f"backend={fresh.backend_name} 键={retrieval_keys[0][:32]}… 命中={'是' if payload is not None else '否'}")

        log("\n=== 4. 提交限流是全局计数（键在 Redis）===")
        codes = [
            client.post("/api/v1/tasks", headers=pm, json={"query": QUESTION, **FILTERS}).status_code
            for _ in range(5)
        ]
        rate_keys = cache.keys("pic:rate:*")
        check("超过上限后返回 429", 429 in codes, f"状态码={codes}")
        check("限流计数写在 Redis（多副本共享同一计数）", len(rate_keys) > 0,
              f"键数={len(rate_keys)} 值={[cache.get(k) for k in sorted(rate_keys)[:2]]}")
        check("限流按租户+用户隔离（admin 不受 pm 计数影响）",
              client.post("/api/v1/tasks", headers=admin, json={"query": QUESTION, **FILTERS}).status_code == 202,
              "admin 仍可提交")

        log("\n=== 5. 人工审核恢复走共享断点（由另一个 worker 进程继续）===")
        waiting = [tid for tid in all_ids
                   if client.get(f"/api/v1/tasks/{tid}", headers=admin).json()["status"] == "waiting_human"]
        if waiting:
            target = waiting[0]
            with engine.connect() as conn:
                before_worker = conn.execute(
                    text("SELECT claimed_by FROM task_queue_items WHERE task_id = :t AND kind = 'run'"),
                    {"t": target},
                ).scalar()
            approved = client.post(f"/api/v1/tasks/{target}/approve", headers=reviewer,
                                   json={"action": "approve", "comment": "多副本验收：跨进程恢复"})
            check("审核接口接受", approved.status_code == 200, str(approved.status_code))
            final = wait_terminal(client, admin, target)
            check("审核后由 worker 继续跑完", final == "completed", f"最终状态={final}")
            with engine.connect() as conn:
                resume_row = conn.execute(
                    text("SELECT kind, status, claimed_by FROM task_queue_items "
                         "WHERE task_id = :t AND kind = 'resume'"),
                    {"t": target},
                ).first()
            check("存在 resume 队列项且已完成", resume_row is not None and resume_row[1] == "done",
                  f"{resume_row}")
            if resume_row and before_worker:
                log(f"      run 由 {before_worker} 执行，resume 由 {resume_row[2]} 执行")
        else:
            log("  [note] 本批没有任务进入人工审核，跳过")

        log("\n=== 6. 成本核对（stub 模型，不应产生费用）===")
        cost = client.get("/api/v1/costs/summary", headers=admin).json()
        check("模型调用成本为 0", float(cost["cost"]["amount"]) == 0.0,
              f"金额={cost['cost']['amount']} 调用={cost['cost']['calls']}")

    failed = [item for item in RESULTS if not item[1]]
    log(f"\n=== 汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    if failed:
        log("失败项：" + "、".join(item[0] for item in failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
