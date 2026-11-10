"""带"挂起即打印全部线程栈"的 pytest 包装器。

为什么需要它：本项目踩过一次**测试整体挂死**（Redis 客户端没有 socket 超时，
连不上时在 `Cache.__init__` 里无限等待），`pytest` 只表现为"没输出、超时被杀"，
连是哪个用例挂住都看不出来。这个包装器在超时后 dump 所有线程栈再退出，
一眼就能看到卡在哪一行——定位那个问题只花了不到一分钟。

用法（在 `backend/` 下）::

    python scripts/pytest_with_dump.py tests -q            # 默认 120 秒后 dump
    python scripts/pytest_with_dump.py tests -q --dump-after 60
    python scripts/pytest_with_dump.py tests/unit -q       # 参数原样透传给 pytest

退出码：pytest 的退出码原样返回；超时被 dump 的情况由进程直接退出（非 0）。
"""

from __future__ import annotations

import faulthandler
import sys

import pytest


def main(argv: list[str]) -> int:
    args = list(argv)
    delay = 120.0
    if "--dump-after" in args:
        index = args.index("--dump-after")
        try:
            delay = float(args[index + 1])
        except (IndexError, ValueError):
            print("--dump-after 需要一个秒数，例如 --dump-after 60", file=sys.stderr)
            return 2
        del args[index : index + 2]

    # 提示走 stdout：PowerShell 把子进程的 stderr 当成错误记录，会污染日志与退出码判定
    print(f"[pytest_with_dump] {delay:.0f} 秒无结束时将打印全部线程栈")
    faulthandler.dump_traceback_later(delay, exit=True)
    try:
        return int(pytest.main(args))
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
