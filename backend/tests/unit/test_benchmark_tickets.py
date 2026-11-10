"""压测脚本 `scripts/benchmark_tickets.py` 的单元测试（不跑真压测）。

压测数字本身要靠脚本运行，但**口径**必须能被回归住，否则它会悄悄跑偏：

1. `FactIncomplete`（缺订单号）必须计入 skipped，而不是错误率 —— 需求书把它定义成
   "退回客户补信息"，是预期内结果，混进错误率会冤枉系统；
2. 节点失败必须计入错误率 —— 工作流把异常**内部收敛**成 `task.status=failed`，
   只看 worker 有没有抛异常会把所有节点失败都算成成功；
3. 验收门槛必须真的能 FAIL —— 一个永远 PASS 的验收脚本等于没有验收；
4. 临时库必须先删重建（含 -wal/-shm）—— 只删 .db 会留下属于旧库的 WAL 文件；
   这条也是"绝不碰开发库"的实现细节。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

BACKEND = Path(__file__).resolve().parents[2]
SCRIPT_PATH = BACKEND / "scripts" / "benchmark_tickets.py"


def _load_script() -> ModuleType:
    """按路径加载脚本（`scripts/` 不是包，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("benchmark_tickets_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load_script()


# --------------------------------------------------------------------- 分位数
def test_percentile_uses_nearest_rank_index():
    """口径：下标 = round(ratio * (n-1))，与 `benchmark_retrieval.py` 完全一致。

    n=100、ratio=0.5 时 round(49.5)=50（Python 的「四舍六入五取偶」），因此中位数取第 51 个元素，
    而不是第 50 个 —— 这个细节会直接进验收报表，所以用测试固定住，避免以后"顺手改成别的口径"。
    """
    values = [float(index) for index in range(1, 101)]  # 1..100
    assert bench.percentile(values, 0.50) == 51.0
    assert bench.percentile(values, 0.95) == 95.0
    assert bench.percentile(values, 0.99) == 99.0
    assert bench.percentile([], 0.95) == 0.0


# --------------------------------------------------------------------- 结局归类
def _result(ticket_id: str, *, latency: float = 100.0, skipped: str | None = None, error: str | None = None) -> dict:
    return {"ticket_id": ticket_id, "latency_ms": latency, "skipped_reason": skipped, "error": error}


def test_fact_incomplete_is_skipped_not_error():
    """缺订单号 → skipped；节点失败/未建运行 → errors；正常完成 → ok。"""
    results = [
        _result("t-ok"),
        _result("t-missing-order", skipped="缺信息：订单号"),
        _result("t-node-failed"),
        _result("t-no-task"),
        _result("t-worker-error", error="OperationalError: database is locked"),
    ]
    db = {
        "status_by_ticket": {
            "t-ok": "completed",
            "t-node-failed": "failed",
            "t-worker-error": "pending",  # worker 抛异常时任务还没跑完
        },
        "error_by_ticket": {"t-node-failed": "TimeoutError: QueuePool limit of size 5 ..."},
    }

    merged = bench.merge_outcomes(results, db)

    assert [item["ticket_id"] for item in merged["ok"]] == ["t-ok"]
    assert [item["ticket_id"] for item in merged["skipped"]] == ["t-missing-order"]
    assert sorted(item["ticket_id"] for item in merged["errors"]) == [
        "t-no-task",
        "t-node-failed",
        "t-worker-error",
    ]
    # 错误详情要能看出是"运行结局失败"还是"worker 抛异常"，否则排障没有抓手
    details = {item["ticket_id"]: item["detail"] for item in merged["errors"]}
    assert "failed" in details["t-node-failed"] and "QueuePool" in details["t-node-failed"]
    assert "database is locked" in details["t-worker-error"]
    assert "未创建分析运行" in details["t-no-task"]


def test_waiting_human_counts_as_success():
    """等待人工审核是业务上的正常中断（不是失败）。"""
    merged = bench.merge_outcomes([_result("t-human")], {"status_by_ticket": {"t-human": "waiting_human"}, "error_by_ticket": {}})
    assert len(merged["ok"]) == 1 and not merged["errors"]


# --------------------------------------------------------------------- 本地形态诊断
def test_diagnose_hints_flags_local_shape_limits():
    hints = bench.diagnose_hints("TimeoutError: QueuePool limit of size 5 overflow 10 reached")
    assert any("连接池" in hint for hint in hints)
    hints = bench.diagnose_hints("OperationalError: database is locked")
    assert any("database is locked" in hint for hint in hints)
    assert bench.diagnose_hints("ValueError: 完全无关的业务错误") == []


# --------------------------------------------------------------------- 验收门槛
def _metrics(p95_ms: float, error_rate: float, *, errors: int = 0, finished: int = 100) -> dict:
    return {
        "latency_ms": {"p50": 100.0, "p95": p95_ms, "p99": p95_ms * 1.2, "min": 10.0, "max": p95_ms * 1.5, "mean": 200.0},
        "throughput_per_minute": 60.0,
        "daily_capacity": 86400.0,
        "error_rate": error_rate,
        "counts": {"finished": finished, "ok": finished - errors, "skipped": 0, "errors": errors},
        "tokens": {"total": 0, "per_ticket": 0.0, "calls": 0, "cache_hit_tokens": 0, "unpriced_models": []},
        "cost": {"total": 0.0, "per_ticket": 0.0, "currency": "CNY"},
    }


def _checks(p95_ms: float, error_rate: float, capsys) -> dict[str, bool]:
    args = bench.parse_args([])
    checks, _ = bench.print_acceptance(_metrics(p95_ms, error_rate), args=args, db={"calls": 0})
    capsys.readouterr()  # 压掉表格输出，避免污染 pytest 的安静输出
    return {name: passed for name, passed, _ in checks}


def test_acceptance_passes_inside_budget(capsys):
    result = _checks(2_000.0, 0.0, capsys)
    assert all(result.values()), result


def test_acceptance_fails_on_slow_p95(capsys):
    """P95 超过 20 秒必须 FAIL（这是需求书的硬指标）。"""
    result = _checks(25_000.0, 0.0, capsys)
    assert any(not passed for name, passed in result.items() if "P95" in name), result


def test_acceptance_fails_on_error_rate(capsys):
    result = _checks(1_000.0, 0.02, capsys)
    assert any(not passed for name, passed in result.items() if "错误率" in name), result


# --------------------------------------------------------------------- 临时库纪律
def test_prepare_env_rebuilds_temp_db_and_never_touches_dev_db(tmp_path, monkeypatch):
    """先删重建：连 -wal/-shm 一起清；DATABASE_URL 指向临时库而不是开发库。"""
    db_path = tmp_path / "ticket_bench.db"
    db_path.write_bytes(b"stale")
    wal = Path(f"{db_path}-wal")
    shm = Path(f"{db_path}-shm")
    wal.write_bytes(b"stale")
    shm.write_bytes(b"stale")

    for key in (
        "DATABASE_URL",
        "CACHE_BACKEND",
        "STORAGE_BACKEND",
        "STORAGE_LOCAL_DIR",
        "CHECKPOINTER_BACKEND",
        "VECTOR_BACKEND",
        "AUTO_INIT_DB",
        "AUTO_SEED",
        "JWT_SECRET",
        "DEBUG",
    ):
        monkeypatch.setenv(key, "sentinel")  # 由 monkeypatch 负责用完还原

    import os

    bench.prepare_env(db_path, tmp_path / "objects")

    assert not db_path.exists() and not wal.exists() and not shm.exists()
    assert os.environ["DATABASE_URL"] == f"sqlite:///{db_path.as_posix()}"
    assert os.environ["CACHE_BACKEND"] == "memory"
    assert os.environ["CHECKPOINTER_BACKEND"] == "memory"
    assert "pic.db" not in os.environ["DATABASE_URL"]


def test_module_pins_offline_providers_before_app_import():
    """模块导入期就钉死 stub 与同步执行（否则闸门与延迟口径都失效）。"""
    import os

    assert os.environ["LLM_PROVIDER"] == "stub"
    assert os.environ["EMBEDDING_PROVIDER"] == "stub"
    assert os.environ["TASK_RUN_INLINE"] == "true"
