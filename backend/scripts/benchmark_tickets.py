"""售后工单分析并发压测：P50/P95/P99 延迟、吞吐、错误率、单均 token 与成本。

脚本回答需求书 §8 那条可验收的指标：**5000 单/天的场景下，单次分析 P95 ≤ 20 秒**，
并附一条工程纪律：错误率 ≤ 1%，任一项不达标就以非 0 退出码结束（可直接进 CI）。

## 并发是怎么建模的（为什么用线程池而不是起 8 个进程）

压测把 `TASK_RUN_INLINE=true`，于是 `start_analysis()` 在**调用它的线程里同步跑完
整条 7-Agent 链路**（建任务 → 补事实 → LangGraph 拓扑 → 落建议）。在此基础上开
`--concurrency` 个线程，每个线程独立建 Session、独立领一张工单：

    线程池 N 线程  ≈  消息队列 + N 个 worker 并发消费

真实生产是「任务队列（`TASK_EXECUTION_BACKEND=queue`）+ 多副本 worker」，两者在
**"一条消息由一个执行单元从头独占跑完"** 这一点上等价，因此延迟口径可比。差异必须说清：

1. **共享一把 GIL**：本脚本所有 worker 在同一个进程里，stub 路径又以 CPU 密集的
   Python 计算为主（哈希向量 + 余弦），所以并发度提上去**换不到线性吞吐**；
   真实 worker 是独立进程/机器，单条延迟更低、总吞吐更高。本脚本的并发数字因此
   **对延迟偏悲观、对吞吐偏保守**。本机一次 200 单的实测很直白：并发 4 → 369 单/分钟、
   P95 1.38s；并发 8 → 180 单/分钟、P95 3.50s（吞吐反而腰斩，多出来的线程只是在抢
   GIL、写锁与连接池）。
2. **不含排队时间**：这里是"worker 一空就立刻领下一单"，生产中一条工单还要在队列里
   等前面排完，真实 P95 = 排队 + 分析，本脚本只测了后一半。
3. **单机 SQLite**：全库只有一个写者（WAL + busy_timeout=30s 只是把等待变成排队），
   生产是 Postgres；所以并发压测里出现 `database is locked` 属于**本地形态的产物**，
   脚本会把它计入错误率并提示降并发，而不是掩盖。
4. **stub 模型**：不调用任何真实模型，延迟里**没有网络与推理耗时**。因此这里的 P95
   只能证明"编排 + 检索 + 落库链路"的上限，**不能当作真实模型下的性能承诺**；
   真实模型下模型调用会占大头（且全局并发还被 `MODEL_MAX_CONCURRENCY` 限制），
   要那个数字必须显式加 `--allow-real-models` 单独重测。
5. **本地连接池也是天花板**：SQLite 引擎用 SQLAlchemy 默认的 QueuePool（5+10=15 条连接），
   而"记账型写入"是进程内串行的（`app/db/session.py::write_scope`）。并发一高就会出现
   "线程握着节点会话等写锁"的互相等待，长尾以 **30 秒连接超时**的形式暴露。
   本机实测这条线是**间歇性踩到**的：并发 8 的 200 单里出现过 1 单这样失败、P99 冲到 31 秒，
   另一次同样配置却零错误、P99 = 3.2 秒；并发 4 时两次运行都零错误。
   脚本会把这类错误单独提示成"本地形态限制"，但它确实计入错误率 —— 不掩盖。

## 安全纪律（两条硬约束）

- **绝不调用真实模型**：import `app.*` 之前就把 `LLM_PROVIDER=stub` 钉死；
  若检测到实际配置是真实模型（`llm_provider != "stub"` 且有 `llm_api_key`），
  默认**拒绝运行**并提示加 `--allow-real-models`（与 `run_ticket_evaluation.py`、
  `demo_tickets.py` 同一道闸门）。
- **绝不碰开发库**：压测跑在独立临时库 `backend/.tmp/ticket_bench.db`（先删旧文件重建，
  含 -wal/-shm），对象存储也改到 `.tmp` 下，`CACHE_BACKEND=memory` 避免写 `data/cache.db`。
  开发库 `data/pic.db` 与 `backend/.env` 全程只读。

## 退出码

- `0`：全部验收项 PASS；`1`：有验收项 FAIL；`2`：拒绝运行（真实模型 / 执行后端不是 inline / 缺种子数据）。

用法::

    python scripts/benchmark_tickets.py                        # 默认 200 单、并发 8
    python scripts/benchmark_tickets.py --count 40 --concurrency 8   # 小规模快速验证
    python scripts/benchmark_tickets.py --json-out .tmp/bench.json   # 落一份结果 JSON
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

# ---------------------------------------------------------------- 环境钉死（必须在 import app.* 之前）
# `get_settings()` 有 lru_cache，导入 app 之后再改环境变量一律无效，所以这三项放在模块顶层。
# 用 setdefault：如果调用方**显式**在 shell 里导出了真实模型配置，这里不覆盖它 ——
# 那样下面的闸门才能生效（否则这道闸门永远打不开，等于没有）。
os.environ.setdefault("LLM_PROVIDER", "stub")
os.environ.setdefault("EMBEDDING_PROVIDER", "stub")
os.environ.setdefault("TASK_RUN_INLINE", "true")

TMP_DB = BACKEND / ".tmp" / "ticket_bench.db"
TMP_OBJECTS = BACKEND / ".tmp" / "ticket_bench_objects"

BENCH_PREFIX = "BENCH-"

# 订单号池：6 个演示订单（`app/adapters/mock.py` 的确定性 fixture，故事完整）+ 24 个派生订单号。
# 为什么轮转而不是一单一个号：真实售后里"一个订单被反复投诉"是常态，池化能顺带压到这条路径。
# 为什么池化**不会**造成大面积 skipped：事实是**按 ticket_id 落库**的（`OrderFact.ticket_id`），
# 适配器又是纯函数（同一订单号永远返回同一份 fixture，不写任何共享状态），所以并发读同一个
# 订单号不会产生写冲突。skipped 只会来自脚本**故意**造的那批缺订单号工单（见 `--incomplete-every`）。
ORDER_POOL_SIZE = 30

# 参考口径（只用于"接入真实模型要花多少钱"的推算，不是实测）：
# 项目此前在真实模型下实测单次分析约 18.3k tokens / ¥0.13
# （见 `app/services/ticket_observability.py::estimate_batch_cost` 的注释与 README 成本章节）。
# stub 模式不调用模型、库里如实记 0 token，这个推算让"5000 单/天多少钱"仍然有答案，且被明确标注。
REFERENCE_TOKENS_PER_TICKET = 18_300
REFERENCE_COST_PER_TICKET = 0.13

# 六类真实售后场景（与演示工单同一套故事线），按序轮转——每张工单文案带唯一序号，
# 避免"同一文本命中检索缓存"把延迟测成缓存读取速度。
TICKET_TEMPLATES: tuple[dict, ...] = (
    {
        "subject": "包裹显示签收，但实际未收到",
        "content": "物流显示已于昨天签收（本人签收），但我没有收到包裹，也没有接到快递电话，请帮我核实。",
        "priority": "high",
    },
    {
        "subject": "购买两件，包裹中仅收到一件",
        "content": "我买了两件猫砂，包裹里只收到一件，包装重量也和订单标注的不一样，请补发缺的那件。",
        "priority": "high",
    },
    {
        "subject": "申请退货退款，商品未拆封",
        "content": "商品不想要了，未拆封，申请退货退款，请问怎么寄回、运费谁承担。",
        "priority": "medium",
    },
    {
        "subject": "收到商品外包装破损，担心影响使用",
        "content": "收到猫爬架时外包装已经破损，里面有磕碰痕迹，担心影响使用，希望换货或补偿。",
        "priority": "medium",
    },
    {
        "subject": "活动页显示满 199 减 30，结算未生效",
        "content": "活动页显示满 199 减 30，我结算时没有减，请帮我看看是不是优惠规则有问题。",
        "priority": "low",
    },
    {
        "subject": "物流超过 48 小时没有更新",
        "content": "包裹物流超过 48 小时没有更新了（当前已停滞 56 小时），麻烦帮我催一下，并说明是否达到补偿标准。",
        "priority": "high",
    },
)


# --------------------------------------------------------------------------- 环境
def prepare_env(db_path: Path, objects_dir: Path) -> None:
    """在 import app.* 之前钉死环境：独立临时库 + 离线模型 + 全部产物落在 .tmp 内。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # 连 -wal/-shm 一起删：只删 .db 会留下属于旧库的 WAL 文件（上一次压测的残留）
    for suffix in ("", "-wal", "-shm"):
        stale = Path(f"{db_path}{suffix}")
        if stale.exists():
            stale.unlink()

    os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
    # 强制（而不是 setdefault）以下各项：压测的产物必须全部落在 .tmp 里，且必须离线。
    os.environ["CACHE_BACKEND"] = "memory"  # 否则会写 data/cache.db（开发产物）
    os.environ["STORAGE_BACKEND"] = "local"
    os.environ["STORAGE_LOCAL_DIR"] = str(objects_dir)  # 播种文档的对象存储也搬进 .tmp
    # checkpointer 固定 memory：压测不涉及"人工审核后恢复"，且 sqlite 后端会把
    # checkpoints.db 写到数据库同目录（那是开发产物目录）。并发下 checkpoint 只写不读，
    # 换来的代价见 docstring 的局限说明。
    os.environ["CHECKPOINTER_BACKEND"] = "memory"
    # 不挂在某个真实向量库上：auto → SQLite 场景自动落到 Python 余弦实现
    os.environ["VECTOR_BACKEND"] = "auto"
    os.environ["AUTO_INIT_DB"] = "true"
    os.environ["AUTO_SEED"] = "true"
    os.environ["JWT_SECRET"] = "ticket-bench-secret-at-least-32-bytes-x"
    os.environ["DEBUG"] = "false"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="benchmark_tickets.py",
        description=(
            "售后工单分析并发压测（离线 stub，绝不调用真实模型）。"
            "打印 P50/P95/P99 延迟、吞吐、错误率、单均 token 与成本，并按验收指标输出 PASS/FAIL。"
        ),
        epilog=(
            "示例：\n"
            "  python scripts/benchmark_tickets.py --count 40 --concurrency 8\n"
            "  python scripts/benchmark_tickets.py --count 200 --concurrency 8 --json-out .tmp/bench.json\n"
            "退出码：0=全部 PASS，1=有 FAIL，2=拒绝运行（真实模型/执行后端不对/缺种子数据）"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--count", type=int, default=200, help="压测工单数（默认 200）")
    parser.add_argument("--concurrency", type=int, default=8, help="并发度 = 模拟的 worker 数（默认 8）")
    parser.add_argument(
        "--target-p95", type=float, default=20.0, help="验收的 P95 延迟上限，单位秒（默认 20.0）"
    )
    parser.add_argument(
        "--daily-volume", type=int, default=5000, help="用于成本与容量推算的日工单量（默认 5000）"
    )
    parser.add_argument("--json-out", default=None, help="把结果另存一份 JSON（便于贴报告）")
    parser.add_argument("--max-error-rate", type=float, default=0.01, help="验收的错误率上限（默认 0.01 = 1%%）")
    parser.add_argument(
        "--warmup",
        type=int,
        default=3,
        help="预热单数：顺序执行且**不计入统计**（排除图编译/连接池/页缓存等首次开销，默认 3）",
    )
    parser.add_argument(
        "--incomplete-every",
        type=int,
        default=20,
        help=(
            "每 N 张工单故意留空订单号，用来验证「缺信息 → 预期内跳过」被计入 skipped 而非 errors"
            "（默认 20；0 = 关闭，全部工单都带有效订单号）"
        ),
    )
    parser.add_argument("--tenant", default="demo", help="租户 code（默认 demo）")
    parser.add_argument("--username", default="ops", help="发起分析的用户（默认 ops）")
    parser.add_argument(
        "--reference-tokens-per-ticket",
        type=int,
        default=REFERENCE_TOKENS_PER_TICKET,
        help="真实模型参考口径：单均 token（默认 18300，仅用于成本推算）",
    )
    parser.add_argument(
        "--reference-cost-per-ticket",
        type=float,
        default=REFERENCE_COST_PER_TICKET,
        help="真实模型参考口径：单均成本元（默认 0.13，仅用于成本推算；0 = 不推算）",
    )
    parser.add_argument("--keep-db", action="store_true", help="保留临时库与对象目录便于排查")
    parser.add_argument(
        "--allow-real-models",
        action="store_true",
        help="确认在已配置真实模型时也运行（**会产生实际费用**，默认拒绝）",
    )
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- 统计工具
def percentile(values: list[float], ratio: float) -> float:
    """分位数（与 `benchmark_retrieval.py` 同一口径：下标 = round(ratio*(n-1))）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[index]


def _line(label: str, value: object, width: int = 30) -> None:
    print(f"  {label:<{width}} {value}")


# --------------------------------------------------------------------------- 造数
def build_tickets(
    session, *, tenant_id: str, user_id: str, count: int, incomplete_every: int, order_pool: list[str]
) -> tuple[list[str], int]:
    """预建压测工单（准备阶段，不计时）。

    直接走 ORM 批量写入而不是 `TicketService.create`：准备阶段的审计行与幂等键对压测毫无价值，
    只会让"建 200 张单"变成另一件要调优的事。工单字段与 `app/db/seed.py::seed_tickets` 对齐，
    保证分析链路看到的是**和生产一样的工单形态**（订单号 + 客户原话 + shop/platform 属性）。
    """
    from app.models.enums import MessageSender, TicketStatus
    from app.models.ticket import Ticket, TicketMessage

    ticket_ids: list[str] = []
    incomplete = 0
    for index in range(count):
        template = TICKET_TEMPLATES[index % len(TICKET_TEMPLATES)]
        # 每 N 张留空订单号：这是**故意**造的"缺信息"工单，用来验证它被计入 skipped 而不是 errors
        missing_order = incomplete_every > 0 and (index + 1) % incomplete_every == 0
        order_id = None if missing_order else order_pool[index % len(order_pool)]
        if missing_order:
            incomplete += 1
        serial = f"{index + 1:05d}"
        content = f"{template['content']}（压测单号 {BENCH_PREFIX}{serial}）"
        ticket = Ticket(
            tenant_id=tenant_id,
            external_id=f"{BENCH_PREFIX}{serial}",
            customer_id=f"U-{BENCH_PREFIX}{serial}",
            order_id=order_id,
            shop_id="self_operated",
            platform="Android",
            subject=template["subject"],
            content=content,
            channel="manual",
            priority=template["priority"],
            status=TicketStatus.NEW.value,
            created_by=user_id,
            assignee_id=user_id,
            attributes={"shop_id": "self_operated", "platform": "Android", "bench": True},
        )
        session.add(ticket)
        session.flush()
        session.add(
            TicketMessage(
                ticket_id=ticket.id,
                sender_type=MessageSender.CUSTOMER.value,
                content=content,
                visible_to_customer=True,
                meta={"bench": True},
            )
        )
        ticket_ids.append(ticket.id)
        if len(ticket_ids) % 50 == 0:
            session.commit()
    session.commit()
    return ticket_ids, incomplete


# --------------------------------------------------------------------------- 单个压测单元
def run_one(ticket_id: str, *, user_id: str) -> dict:
    """跑一张工单（= 一个 worker 领一条队列消息），返回该次的观测结果。

    两件事只在这里做，都是为了"一次运行失败不会连累整个脚本"：
    1. **每线程独立 Session**：`Session` 不是线程安全的，跨线程共享会得到
       "另一个线程正在使用此连接"这类莫名其妙的错；
    2. 所有异常就地兜住并落成 `error` 字段，由主线程统一计入错误率（脚本不崩）。
    """
    from app.db.session import SessionLocal
    from app.models.identity import User
    from app.models.ticket import Ticket
    from app.models.workflow import AnalysisTask
    from app.services.ticket_analysis import FactIncomplete, start_analysis

    session = SessionLocal()
    result: dict = {"ticket_id": ticket_id, "latency_ms": 0.0, "skipped_reason": None, "error": None}
    try:
        ticket = session.get(Ticket, ticket_id)
        user = session.get(User, user_id)
        if ticket is None or user is None:
            result["error"] = f"工单或用户不存在（ticket={ticket_id}）"
            return result

        started = time.perf_counter()
        try:
            # 唯一入口：补事实 → 建 AnalysisTask → 派发执行。
            # inline 后端下这一步会**同步跑完**整条链路，所以墙钟时间就是端到端分析延迟。
            task_id = start_analysis(session, ticket=ticket, user=user)
        except FactIncomplete as exc:
            # 缺订单号/订单不存在/接口超时 → 预期内的"退回客户补信息"，不是系统错误
            result["latency_ms"] = (time.perf_counter() - started) * 1000
            result["skipped_reason"] = "缺信息：" + "、".join(exc.missing or ["未知"])
            return result
        finally:
            if not result["latency_ms"]:
                result["latency_ms"] = (time.perf_counter() - started) * 1000

        # 工作流在同线程内跑完后，状态是**别的会话**提交的：必须过期重读，
        # 否则 identity map 里那条 pending 的对象会一直骗我们说"还没开始"
        # （`demo_tickets.py` 里同样的 expire_all，是踩过之后加的）。
        session.expire_all()
        task = session.get(AnalysisTask, task_id)
        result["task_id"] = task_id
        result["task_status"] = task.status if task is not None else None
        result["task_error"] = (task.error or {}).get("code") if task is not None else None
        return result
    except Exception as exc:  # noqa: BLE001 - 压测脚本绝不允许被单张工单带崩
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        session.close()


# --------------------------------------------------------------------------- 聚合
def collect_db_metrics(session, *, tenant_id: str, ticket_ids: list[str]) -> dict:
    """从库里统计运行结局与 token/成本（口径与界面一致：`ticket_cost`）。"""
    from app.models.ticket import Ticket
    from app.models.workflow import AnalysisTask
    from app.services.ticket_observability import ticket_cost

    wanted = set(ticket_ids)
    status_by_ticket: dict[str, str] = {}
    error_by_ticket: dict[str, str] = {}
    # 临时库是本次运行专用的，按租户全取即可；再按 filters.ticket_id 归位到具体工单
    for task in session.query(AnalysisTask).filter(AnalysisTask.tenant_id == tenant_id).all():
        ticket_id = (task.filters or {}).get("ticket_id")
        if ticket_id in wanted:
            status_by_ticket[ticket_id] = task.status
            if task.error:
                # 错误码 + 消息都留着：形如 TimeoutError / database is locked 的**消息**才是
                # 判断"是本地形态限制还是真缺陷"的唯一线索（见 diagnose_hints）
                code = str(task.error.get("code") or "unknown")
                message = str(task.error.get("message") or "")[:200].replace("\n", " ")
                error_by_ticket[ticket_id] = f"{code}: {message}" if message else code

    # token/成本：逐张工单调 `ticket_cost`（累计口径，含每模型明细）；
    # stub 下没有模型调用，这里会如实得到 0 —— 不是脚本没测，而是真的没花钱。
    total_tokens = 0
    total_amount = 0.0
    calls = 0
    cache_hit_tokens = 0
    analyses = 0
    unpriced: set[str] = set()
    for ticket in session.query(Ticket).filter(Ticket.id.in_(sorted(wanted))).all():
        payload = ticket_cost(session, ticket)
        analyses += int(payload.get("analysis_count") or 0)
        total_tokens += int(payload.get("total_tokens") or 0)
        total_amount += float(payload.get("amount") or 0.0)
        calls += int(payload.get("calls") or 0)
        cache_hit_tokens += int(payload.get("cache_hit_tokens") or 0)
        unpriced |= set(payload.get("unpriced_models") or [])

    return {
        "status_by_ticket": status_by_ticket,
        "error_by_ticket": error_by_ticket,
        "analyses": analyses,
        "total_tokens": total_tokens,
        "total_amount": round(total_amount, 6),
        "calls": calls,
        "cache_hit_tokens": cache_hit_tokens,
        "unpriced_models": sorted(unpriced),
    }


def diagnose_hints(*blobs: str) -> list[str]:
    """从错误文本里认出"本地形态限制"而不是"业务缺陷"，并给出可执行的下一步。

    这两条都是**单进程 + 单机 SQLite** 压测特有的：
    - `QueuePool limit ...`：SQLite 引擎用的是 SQLAlchemy 默认连接池（pool_size=5 + overflow=10
      = 15 条，`app/db/session.py` 只对非 SQLite 方言应用 db_pool_size）。并发度上去后，
      线程会一边握着节点会话、一边等写锁（`write_scope` 是进程内串行），最后连池子一起耗尽，
      于是长尾以 **30 秒连接超时**的形式出现。
    - `database is locked`：SQLite 只允许一个写者，busy_timeout=30s 把锁竞争变成等待。
    两种情况都该降并发复测，而不是去改业务流程。
    """
    text = " ".join(part for part in blobs if part)
    hints: list[str] = []
    if "QueuePool limit" in text or "connection timed out" in text:
        hints.append(
            "连接池耗尽（SQLite 走 SQLAlchemy 默认 QueuePool：5+10=15 条连接）→ "
            "本地压测请降低 --concurrency（实测 8 会触顶，4 不会）；生产用 Postgres 时连接池可配。"
        )
    if "database is locked" in text:
        hints.append(
            "出现 database is locked（单机 SQLite 只允许一个写者）→ 降低 --concurrency 复测；"
            "生产用 Postgres 时这条长尾不成立。"
        )
    return hints


def merge_outcomes(results: list[dict], db: dict) -> dict:
    """把"worker 观测"与"库里的运行结局"合成成功/跳过/错误三类。

    为什么要两边一起看：工作流把节点异常**内部收敛**成 `task.status=failed`
    （不向外抛），只信 worker 的异常捕获会把所有节点失败都算成成功。
    """
    ok: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    for item in results:
        ticket_id = item["ticket_id"]
        status = db["status_by_ticket"].get(ticket_id)
        if item.get("skipped_reason"):
            skipped.append({**item, "detail": item["skipped_reason"]})
        elif status in ("failed", "cancelled"):
            errors.append(
                {
                    **item,
                    "detail": f"运行结局={status}（{db['error_by_ticket'].get(ticket_id) or '无错误码'}）",
                }
            )
        elif item.get("error"):
            errors.append({**item, "detail": item["error"]})
        elif status in ("completed", "waiting_human"):
            ok.append(item)
        elif status is None:
            errors.append({**item, "detail": "未创建分析运行（start_analysis 未落库）"})
        else:
            errors.append({**item, "detail": f"运行未收敛到终态：{status}"})
    return {"ok": ok, "skipped": skipped, "errors": errors}


# --------------------------------------------------------------------------- 输出
def print_environment(
    *, settings, backend: str, args, db_path: Path, seed_summary: dict, seed_seconds: float
) -> None:
    from app.graph.checkpointer import describe_checkpointer
    from app.rag.vectorstore import describe_vector_store

    vector = describe_vector_store(settings)
    checkpointer = describe_checkpointer(settings)
    print("=== 环境 ===")
    _line("临时库", f"{db_path}（先删重建；开发库 data/pic.db 未被触碰）")
    _line("模型", f"{settings.llm_provider}（LLM_PROVIDER=stub，零成本、不联网）")
    _line("Embedding / 缓存", f"{settings.embedding_provider} / {settings.cache_backend}（进程内）")
    _line("执行后端", f"{backend}（TASK_RUN_INLINE=true → 线程内同步跑完整 7-Agent 链路）")
    _line("Checkpointer", f"{checkpointer.get('backend')}（压测不恢复中断）")
    _line("向量后端", f"{vector.get('effective') or '未初始化（配置 ' + str(vector.get('configured')) + '）'}")
    _line("并发度", f"{args.concurrency} 线程 ≈ {args.concurrency} 个 worker 并发消费")
    _line("压测工单数", f"{args.count}（另有 {args.warmup} 单预热，不计入统计）")
    _line("验收目标", f"P95 ≤ {args.target_p95}s、错误率 ≤ {args.max_error_rate:.2%}")
    _line("日工单量（推算用）", f"{args.daily_volume} 单/天（一年按 365 天）")
    _line(
        "播种",
        f"文档={seed_summary.get('documents')} 政策={seed_summary.get('policy_documents')} "
        f"演示工单={len(seed_summary.get('tickets') or [])}（耗时 {seed_seconds:.1f}s）",
    )
    _line("运行环境", f"CPython {platform.python_version()} / {os.cpu_count()} 逻辑核")
    print()


def print_metrics_table(*, ok: list[dict], skipped: list[dict], errors: list[dict], elapsed: float, db: dict, args) -> dict:
    latencies = [item["latency_ms"] for item in ok]
    finished = len(ok) + len(skipped) + len(errors)
    error_rate = (len(errors) / finished) if finished else 1.0
    throughput = (len(ok) / elapsed) * 60 if elapsed > 0 else 0.0
    daily_capacity = throughput * 60 * 24
    per_token = (db["total_tokens"] / len(ok)) if ok else 0.0
    per_amount = (db["total_amount"] / len(ok)) if ok else 0.0

    print("=== 压测结果 ===")
    _line("墙钟耗时", f"{elapsed:.2f}s")
    _line("成功分析", f"{len(ok)} 单")
    _line("预期内跳过", f"{len(skipped)} 单（缺订单号 → FactIncomplete，不计入错误）")
    _line("错误", f"{len(errors)} 单")
    print()
    print("=== 指标 ===")
    _line("延迟 P50（毫秒）", f"{percentile(latencies, 0.50):.1f}")
    _line("延迟 P95（毫秒）", f"{percentile(latencies, 0.95):.1f}")
    _line("延迟 P99（毫秒）", f"{percentile(latencies, 0.99):.1f}")
    _line("延迟 均值/最小/最大（毫秒）", f"{statistics.fmean(latencies):.1f} / {min(latencies) if latencies else 0:.1f} / {max(latencies) if latencies else 0:.1f}")
    _line("吞吐（成功单/分钟）", f"{throughput:.1f}")
    _line("吞吐折算日容量（单/天）", f"{daily_capacity:,.0f}（需求 {args.daily_volume:,} 单/天）")
    _line("错误率", f"{error_rate:.2%}（{len(errors)}/{finished}）")
    _line("单均 token（实测记账）", f"{per_token:,.0f}")
    _line("单均成本（实测记账，CNY）", f"{per_amount:.6f}")
    _line("模型调用次数 / 缓存命中 token", f"{db['calls']} / {db['cache_hit_tokens']}")
    _line("分析运行数（库里）", f"{db['analyses']}")
    return {
        "latency_ms": {
            "p50": round(percentile(latencies, 0.50), 2),
            "p95": round(percentile(latencies, 0.95), 2),
            "p99": round(percentile(latencies, 0.99), 2),
            "min": round(min(latencies), 2) if latencies else 0.0,
            "max": round(max(latencies), 2) if latencies else 0.0,
            "mean": round(statistics.fmean(latencies), 2) if latencies else 0.0,
        },
        "throughput_per_minute": round(throughput, 2),
        "daily_capacity": round(daily_capacity, 0),
        "error_rate": round(error_rate, 6),
        "counts": {
            "finished": finished,
            "ok": len(ok),
            "skipped": len(skipped),
            "errors": len(errors),
        },
        "tokens": {
            "total": db["total_tokens"],
            "per_ticket": round(per_token, 1),
            "calls": db["calls"],
            "cache_hit_tokens": db["cache_hit_tokens"],
            "unpriced_models": db["unpriced_models"],
        },
        "cost": {
            "total": db["total_amount"],
            "per_ticket": round(per_amount, 6),
            "currency": settings_currency(),
        },
    }


def settings_currency() -> str:
    from app.config import get_settings

    return get_settings().currency


def print_acceptance(metrics: dict, *, args, db: dict) -> tuple[list[tuple[str, bool, str]], dict]:
    """按需求书口径逐项 PASS/FAIL，并附成本推算。返回 (验收项, 推算明细)。"""
    from app.services.ticket_observability import estimate_batch_cost

    p95_seconds = metrics["latency_ms"]["p95"] / 1000.0
    checks = [
        (
            f"P95 延迟 ≤ {args.target_p95:.0f} 秒（{args.daily_volume} 单/天场景）",
            p95_seconds <= args.target_p95,
            f"实测 P95 = {p95_seconds:.3f}s（余量 {args.target_p95 / max(p95_seconds, 1e-9):.1f}x）",
        ),
        (
            f"错误率 ≤ {args.max_error_rate:.0%}",
            metrics["error_rate"] <= args.max_error_rate,
            f"实测 {metrics['error_rate']:.2%}（{metrics['counts']['errors']}/{metrics['counts']['finished']}）",
        ),
    ]

    print()
    print(f"=== 验收（需求书 §8：{args.daily_volume} 单/天，单次分析 P95 ≤ {args.target_p95:.0f} 秒）===")
    for name, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name} — {detail}")

    # 容量校核（仅提示，不作为 FAIL 门槛）：短时压测外推到"单/天"只能看数量级
    needed_per_minute = args.daily_volume / (24 * 60)
    print(
        f"  [INFO] 容量校核：{metrics['throughput_per_minute']:.1f} 单/分钟 "
        f"（需求 {needed_per_minute:.2f} 单/分钟），"
        f"按当前吞吐外推日容量 ≈ {metrics['daily_capacity']:,.0f} 单/天"
    )
    p99_seconds = metrics["latency_ms"]["p99"] / 1000.0
    if p99_seconds > args.target_p95:
        print(
            f"  [INFO] 注意 P99 = {p99_seconds:.2f}s 已超过 {args.target_p95:.0f}s 预算："
            "需求书口径只约束 P95，但长尾会打穿单张工单的分析超时，上线前必须解释它从哪来"
            "（本地形态下是连接池/GIL 争用，见下方口径局限）"
        )
    print(
        f"  [INFO] 实测记账：单均 {metrics['tokens']['per_ticket']:,.0f} token / "
        f"{metrics['cost']['per_ticket']:.6f} {metrics['cost']['currency']}"
        + ("（stub 不调用模型，库内如实记 0，因此实测成本为 0）" if db["calls"] == 0 else "")
    )

    reference: dict = {}
    if args.reference_cost_per_ticket > 0:
        estimate = estimate_batch_cost(args.reference_cost_per_ticket, args.daily_volume)
        reference = {
            "assumed_tokens_per_ticket": args.reference_tokens_per_ticket,
            "assumed_cost_per_ticket": args.reference_cost_per_ticket,
            "daily_tokens": args.reference_tokens_per_ticket * args.daily_volume,
            **estimate,
        }
        print(
            f"  [INFO] 真实模型参考推算：{args.reference_tokens_per_ticket:,} token/单、"
            f"¥{args.reference_cost_per_ticket}/单 → "
            f"{args.daily_volume:,} 单/天 ≈ ¥{estimate['daily_amount']:,.2f}/天、"
            f"¥{estimate['monthly_amount']:,.2f}/月、¥{estimate['yearly_amount']:,.2f}/年"
        )
        print("         （推算口径来自项目此前的真实模型实测值，**不是本次压测的测量值**）")
    return checks, reference


def print_limitations(args) -> None:
    print()
    print("=== 口径局限（结论必须连着这些一起读）===")
    print("  1. stub 模型：延迟不含网络与真实推理耗时，本次 P95 只证明「编排 + 检索 + 落库链路」的上限，")
    print("     不能当作真实模型下的性能承诺；要真实数字请加 --allow-real-models 重测（会产生费用）。")
    print(f"  2. 共享 GIL：{args.concurrency} 个 worker 全在一个进程内，stub 以 CPU 计算为主，")
    print("     并发换不到线性吞吐；真实 worker 是独立进程/机器（延迟更低、吞吐更高）。")
    print("  3. 不含排队：worker 一空就领下一单，真实生产的 P95 还要叠加队列等待时间。")
    print("  4. 单机 SQLite 单写者 + WAL/busy_timeout=30s：锁竞争被变成等待，")
    print("     并发越高越容易看到长尾；生产用 Postgres，长尾形态不同。")
    print("  5. 连接池上限：SQLite 引擎走 SQLAlchemy 默认 QueuePool（5+10=15 条，")
    print("     app/db/session.py 只对非 SQLite 方言应用 db_pool_size），而短写事务是进程内串行的；")
    print("     并发度越大越容易触顶——线程会一边握着节点会话、一边等写锁，长尾以 30 秒连接超时的形式出现")
    print("     （本机实测：并发 8 的 200 单里出现过 1 单 30 秒超时，但也跑出过零错误的一次；并发 4 两次都零错误。）")
    print("     这是单进程压测的形态限制：降 --concurrency 或用 Postgres 复测，而不是改业务流程。")
    if args.count < 200:
        print(f"  6. 样本量 {args.count} 单：P99 需要 ≥200 个样本才有意义，小样本的 P99 基本等于最大值。")


# --------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    import logging

    args = parse_args(argv)
    if args.count <= 0 or args.concurrency <= 0:
        print("!! --count 与 --concurrency 必须为正整数")
        return 2
    if args.warmup < 0:
        print("!! --warmup 不能为负数")
        return 2

    prepare_env(TMP_DB, TMP_OBJECTS)

    from app.config import get_settings
    from app.db.seed import init_db, seed_all
    from app.db.session import SessionLocal, engine
    from app.models.identity import Tenant, User
    from app.services.task_dispatch import resolve_backend

    settings = get_settings()

    # 脚本用 print 输出人看的表；应用日志压到 WARNING，否则播种那十几行 INFO 会淹没表格。
    # 只压到 WARNING 而不是 ERROR：checkpointer/锁竞争这类警告必须仍然看得见。
    logging.getLogger().setLevel(logging.WARNING)

    # ---- 闸门一：执行后端必须是 inline（否则线程里拿不到"跑完"的结果）
    backend = resolve_backend(settings)
    if backend != "inline":
        print("!! 执行后端不是 inline：异步后端下 start_analysis 立刻返回，")
        print("   脚本量的就不是端到端分析延迟。请确认 TASK_RUN_INLINE 未被别处覆盖后重跑。")
        return 2
    # ---- 闸门二：真实模型必须显式确认（可能花钱的操作一律默认拒绝）
    if settings.llm_provider != "stub" and settings.llm_api_key and not args.allow_real_models:
        print("!! 当前配置会调用真实模型，压测会产生实际费用（按 token 计费，量随 --count 增长）。")
        print("   想要免费的离线压测：不要导出 LLM_PROVIDER=openai_compatible（脚本默认钉死 stub）；")
        print("   确认愿意花钱请加 --allow-real-models 重跑。")
        return 2

    init_db()
    seed_started = time.perf_counter()
    seed_summary = seed_all()
    seed_seconds = time.perf_counter() - seed_started

    session = SessionLocal()
    try:
        tenant = session.query(Tenant).filter(Tenant.code == args.tenant).first()
        user = session.query(User).filter(User.username == args.username).first()
        if tenant is None or user is None:
            print(f"!! 未找到租户 {args.tenant} / 用户 {args.username}，请先执行 python -m app.db.seed")
            return 2

        from app.adapters.mock import demo_order_ids

        order_pool = list(demo_order_ids()) + [
            f"TB-BENCH-{index:06d}" for index in range(ORDER_POOL_SIZE - len(demo_order_ids()))
        ]
        total_tickets = args.count + args.warmup
        ticket_ids, incomplete = build_tickets(
            session,
            tenant_id=tenant.id,
            user_id=user.id,
            count=total_tickets,
            incomplete_every=args.incomplete_every,
            order_pool=order_pool,
        )
        tenant_id, user_id = tenant.id, user.id
    finally:
        session.close()

    print_environment(
        settings=settings,
        backend=backend,
        args=args,
        db_path=TMP_DB,
        seed_summary=seed_summary,
        seed_seconds=seed_seconds,
    )
    print("=== 预建压测工单 ===")
    _line("工单数", f"{len(ticket_ids)} 张（含预热 {args.warmup} 张）")
    _line("订单号池", f"{len(order_pool)} 个，按序轮转（同一订单被多单引用不会写冲突，见脚本注释）")
    _line(
        "缺订单号工单",
        f"{incomplete} 张（--incomplete-every {args.incomplete_every}）→ 预期内跳过",
    )
    print()

    # ---------------------------------------------------------------- 预热（不计入统计）
    # 首次调用要付出：编译两张 LangGraph 图、建 checkpointer、探活缓存后端、SQLite 页缓存、
    # 向量库初始化。不预热的话这些开销会全砸在第一批工单上，把 P95 测成"启动时间"
    # （`benchmark_retrieval.py` 里也是为同一个原因加预热）。
    warmup_ids, bench_ids = ticket_ids[: args.warmup], ticket_ids[args.warmup :]
    if warmup_ids:
        print(f"=== 预热 {len(warmup_ids)} 单（顺序执行，不计入统计）===")
        started = time.perf_counter()
        warm = [run_one(ticket_id, user_id=user_id) for ticket_id in warmup_ids]
        print(f"  完成，耗时 {time.perf_counter() - started:.2f}s")
        bad = [item for item in warm if item.get("error") or item.get("skipped_reason")]
        if bad:
            print(f"  !! 预热期间已有异常（说明环境有问题，先看这里）：{bad[0]}")
        print()

    # ---------------------------------------------------------------- 正式压测
    print(f"=== 压测（并发 {args.concurrency}，{len(bench_ids)} 单）===")
    results: list[dict] = []
    pool_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency, thread_name_prefix="bench") as pool:
        # map 会按提交顺序阻塞取结果；worker 完成一单立刻领下一单，等价于队列 + N 个 worker
        for item in pool.map(lambda tid: run_one(tid, user_id=user_id), bench_ids):
            results.append(item)
    elapsed = time.perf_counter() - pool_started

    # ---------------------------------------------------------------- 聚合与输出
    session = SessionLocal()
    try:
        db = collect_db_metrics(session, tenant_id=tenant_id, ticket_ids=bench_ids)
    finally:
        session.close()
    merged = merge_outcomes(results, db)
    metrics = print_metrics_table(
        ok=merged["ok"], skipped=merged["skipped"], errors=merged["errors"], elapsed=elapsed, db=db, args=args
    )
    checks, reference = print_acceptance(metrics, args=args, db=db)

    if merged["skipped"]:
        print()
        print(f"=== 预期内跳过（{len(merged['skipped'])} 单，最多列 5 条）===")
        for item in merged["skipped"][:5]:
            print(f"  {item['ticket_id']}  {item['detail']}")
    if merged["errors"]:
        print()
        print(f"=== 失败（{len(merged['errors'])} 单，最多列 10 条）===")
        for item in merged["errors"][:10]:
            print(f"  {item['ticket_id']}  {str(item['detail'])[:150]}")
        hints = diagnose_hints(
            *[str(item.get("detail") or "") for item in merged["errors"]],
            *[str(item.get("error") or "") for item in merged["errors"]],
            *db["error_by_ticket"].values(),
        )
        for hint in hints:
            print(f"  提示：{hint}")

    print_limitations(args)

    failed = [name for name, passed, _ in checks if not passed]
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "environment": {
            "database": str(TMP_DB),
            "development_database_untouched": True,
            "llm_provider": settings.llm_provider,
            "embedding_provider": settings.embedding_provider,
            "task_backend": backend,
            "cache_backend": settings.cache_backend,
            "concurrency": args.concurrency,
            "count": args.count,
            "warmup": args.warmup,
            "incomplete_every": args.incomplete_every,
            "daily_volume": args.daily_volume,
            "target_p95_seconds": args.target_p95,
            "max_error_rate": args.max_error_rate,
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "metrics": metrics,
        "wall_seconds": round(elapsed, 3),
        "acceptance": {
            "results": [
                {"name": name, "passed": bool(passed), "detail": detail} for name, passed, detail in checks
            ],
            "passed": not failed,
        },
        "reference_projection": reference,
        "skipped": merged["skipped"],
        "errors": merged["errors"],
    }
    if args.json_out:
        out_path = Path(args.json_out)
        if not out_path.is_absolute():
            out_path = BACKEND / out_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print()
        print(f"  结果 JSON 已写入：{out_path}")

    # ---------------------------------------------------------------- 清理
    if not args.keep_db:
        try:
            engine.dispose()
            for suffix in ("", "-wal", "-shm"):
                stale = Path(f"{TMP_DB}{suffix}")
                if stale.exists():
                    stale.unlink()
            shutil.rmtree(TMP_OBJECTS, ignore_errors=True)
            print(f"  临时库与对象目录已清理（{TMP_DB.name}）")
        except Exception as exc:  # pragma: no cover - 清理失败不影响结论
            print(f"  临时库清理失败：{exc}")

    print()
    print(f"=== 结论：{'PASS' if not failed else 'FAIL'} ===")
    if failed:
        for name in failed:
            print(f"  未达标：{name}")
        return 1
    print("  全部验收项达标（P95 与错误率见上表）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
