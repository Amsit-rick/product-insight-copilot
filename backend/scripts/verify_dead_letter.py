"""死信运维实测：造真死信 → 看它被列出来 → 重放 → 观察它真的被重新领取。

为什么用"指向不存在任务的队列项"来造死信：这是**唯一确定性**的失败方式 ——
worker 领取后 `runner.run()` 会抛 `ValueError: 任务不存在`，nack 会被调用，
重试耗尽（max_attempts=3）后进 dead。用真实任务很难稳定制造"连续 3 次失败"，
而这里要验证的是**队列的判死与重放语义**，不是工作流本身会不会失败。

真死信的另一个来源（worker 反复崩溃导致租约回收耗尽）在 verify_worker_crash.py 里已经验过。

用法（先按 README §7.4.1 起好 Postgres/Redis/API/worker）：

    ..\\.venv\\Scripts\\python.exe scripts\\verify_dead_letter.py

环境变量同 verify_multiprocess.py：MULTI_API / MULTI_PG_URL / MULTI_REDIS。
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

import httpx  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
lines: list[str] = []


def log(message: str = "") -> None:
    lines.append(message)
    print(message)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    log(f"  [{'PASS' if ok else 'FAIL'}] {name}{' — ' + detail if detail else ''}")


def main() -> int:
    engine = create_engine(PG_URL, future=True)

    # 直接塞一条"指向不存在任务"的队列项：worker 领取后必然失败 → 重试耗尽 → 死信
    with engine.begin() as conn:
        bogus_task = "00000000-dead-beef-0000-000000000001"
        item_id = conn.execute(
            text(
                "INSERT INTO task_queue_items (id, task_id, kind, payload, status, priority, "
                "attempts, max_attempts, visible_at, created_at, updated_at) "
                "VALUES (gen_random_uuid()::text, :t, 'run', '{}'::json, 'pending', 0, 0, 3, "
                "now(), now(), now()) "
                "RETURNING id"
            ),
            {"t": bogus_task},
        ).scalar()
    log(f"=== 造一条注定失败的队列项（task_id={bogus_task[:13]}…，指向不存在的任务）===")
    log(f"    item_id = {item_id}")

    with httpx.Client(base_url=API, timeout=60.0) as client:
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "admin123"})
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        log("\n=== 1. 等 worker 把它跑死（重试 3 次后判死）===")
        row = None
        deadline = time.time() + 120
        while time.time() < deadline:
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT status, attempts, last_error FROM task_queue_items WHERE id = :i"),
                    {"i": item_id},
                ).first()
            if row and row[0] == "dead":
                break
            time.sleep(1.5)
        check("队列项最终被判死（dead）", row is not None and row[0] == "dead",
              f"status={row[0]} attempts={row[1]}" if row else "未取到")
        check("尝试次数达到上限", row is not None and row[1] >= 3, f"attempts={row[1] if row else '-'}")
        check("错误原因被记录下来（不是只留一个数字）",
              row is not None and bool(row[2]) and "任务不存在" in (row[2] or ""),
              (row[2] or "")[:90] if row else "")

        log("\n=== 2. 运维接口能看到这条死信（含任务上下文）===")
        listed = client.get("/api/v1/queue/items?status=dead&limit=50", headers=headers)
        check("GET /queue/items 返回 200", listed.status_code == 200, str(listed.status_code))
        body = listed.json()
        found = next((item for item in body["items"] if item["id"] == item_id), None)
        check("死信出现在列表里", found is not None, f"共 {len(body['items'])} 条死信")
        if found:
            check("带出最后错误", "任务不存在" in (found["last_error"] or ""), (found["last_error"] or "")[:70])
            check("标出任务记录已不存在（否则光看 task_id 没法判断）", found["task_missing"] is True,
                  f"task_missing={found['task_missing']}")
        check("统计里 dead 计数正确", body["stats"]["dead"] >= 1, json.dumps(body["stats"], ensure_ascii=False))

        log("\n=== 3. 非管理员不能看 / 不能重放 ===")
        pm_login = client.post("/api/v1/auth/login", json={"username": "pm", "password": "pm123"})
        pm = {"Authorization": f"Bearer {pm_login.json()['access_token']}"}
        check("pm 看队列运维 → 403", client.get("/api/v1/queue/items", headers=pm).status_code == 403)
        check("pm 重放死信 → 403", client.post(f"/api/v1/queue/items/{item_id}/replay", headers=pm).status_code == 403)

        log("\n=== 4. 先验证「非死信不能重放」（拿一条 done 的项试）===")
        with engine.connect() as conn:
            done_id = conn.execute(
                text("SELECT id FROM task_queue_items WHERE status = 'done' LIMIT 1")
            ).scalar()
        if done_id:
            resp = client.post(f"/api/v1/queue/items/{done_id}/replay", headers=headers)
            check("重放 done 项 → 409（避免同一任务两条队列项）",
                  resp.status_code == 409 and resp.json()["detail"]["code"] == "not_dead",
                  f"{resp.status_code} {resp.text[:70]}")

        log("\n=== 5. 重放这条死信 ===")
        resp = client.post(f"/api/v1/queue/items/{item_id}/replay", headers=headers)
        check("重放接口返回 200", resp.status_code == 200, resp.text[:120])
        with engine.connect() as conn:
            after = conn.execute(
                text("SELECT status, attempts, last_error, payload FROM task_queue_items WHERE id = :i"),
                {"i": item_id},
            ).first()
        payload = after[3] if isinstance(after[3], dict) else json.loads(after[3] or "{}")
        check("attempts 被清零（否则一失败立刻又判死）", after[1] == 0, f"attempts={after[1]}")
        check("last_error 保留（重放后仍看得见上次为什么死）",
              "任务不存在" in (after[2] or ""), (after[2] or "")[:60])
        check("重放历史留痕", len(payload.get("replays") or []) == 1,
              json.dumps(payload.get("replays"), ensure_ascii=False)[:120])

        log("\n=== 6. 重放之后 worker 真的把它领走又跑了一遍（重新用满 3 次预算）===")
        row2 = None
        deadline = time.time() + 120
        while time.time() < deadline:
            with engine.connect() as conn:
                row2 = conn.execute(
                    text(
                        "SELECT status, attempts, payload FROM task_queue_items WHERE id = :i"
                    ),
                    {"i": item_id},
                ).first()
            if row2 and row2[0] == "dead" and row2[1] >= 3:
                break
            time.sleep(1.5)
        payload2 = row2[2] if isinstance(row2[2], dict) else json.loads(row2[2] or "{}")
        check("重放后又跑满 3 次并再次判死（说明重放确实给了完整重试预算）",
              row2 is not None and row2[0] == "dead" and row2[1] >= 3,
              f"status={row2[0]} attempts={row2[1]}" if row2 else "未取到")
        # 注意：第二次判死是**正常重试路径**造成的，不是又发生了一次重放 —— 所以重放历史仍是 1 次。
        # （我第一版断言这里该变成 2 次，是我把"再次判死"误当成"再次重放"了。）
        check("重放历史仍为 1 次（第二次判死来自正常重试，不是重放）",
              len(payload2.get("replays") or []) == 1,
              f"replays={len(payload2.get('replays') or [])}")

        log("\n=== 7. 一键全部放回队列 + 审计留痕 ===")
        before = client.get("/api/v1/audit-logs?action=queue_item_replayed&limit=100", headers=headers).json()
        resp = client.post("/api/v1/queue/replay-dead", headers=headers)
        check("一键重放返回 200", resp.status_code == 200, resp.text[:120])
        check("重放数量 >= 1", resp.json()["replayed"] >= 1, f"replayed={resp.json()['replayed']}")
        after_audit = client.get("/api/v1/audit-logs?action=queue_dead_replayed_all&limit=100", headers=headers).json()
        check("一键重放也写审计", len(after_audit["items"]) >= 1, f"audit={len(after_audit['items'])} 条")
        check("单条重放有审计记录", len(before["items"]) >= 1, f"queue_item_replayed={len(before['items'])} 条")
        # 一键重放也是一次真正的重放 → 重放历史应当累加到 2 次
        with engine.connect() as conn:
            payload3 = conn.execute(
                text("SELECT payload FROM task_queue_items WHERE id = :i"), {"i": item_id}
            ).scalar()
        payload3 = payload3 if isinstance(payload3, dict) else json.loads(payload3 or "{}")
        check("一键重放后重放历史累加到 2 次", len(payload3.get("replays") or []) == 2,
              f"replays={len(payload3.get('replays') or [])}")

        log("\n=== 8. 收尾：清掉这条测试死信，别留给下一个跑的人 ===")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM task_queue_items WHERE id = :i"), {"i": item_id})
        log(f"    已删除 {item_id}")

    failed = [item for item in RESULTS if not item[1]]
    log(f"\n=== 汇总：{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过 ===")
    if failed:
        log("失败项：" + "、".join(item[0] for item in failed))

    out = BACKEND / ".tmp" / "dead_letter_out.txt"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nwrote {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
