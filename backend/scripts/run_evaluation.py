"""运行评测集并打印聚合指标（设计文档 16.2 节）。

用法（在 backend 目录下）：

    $env:PYTHONPATH = $PWD
    ..\\.venv\\Scripts\\python.exe scripts\\run_evaluation.py            # 全量 50 条
    ..\\.venv\\Scripts\\python.exe scripts\\run_evaluation.py --limit 8  # 快速回归
    ..\\.venv\\Scripts\\python.exe scripts\\run_evaluation.py --category conflict_evidence

评测会**真实执行完整工作流**（并行 Agent → 证据合并 → 分支判定 → 报告），
因此耗时与用例数成正比；默认模型为 stub（离线确定性归纳），结果可重复。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.models.identity import Tenant  # noqa: E402
from app.services.evaluation_service import EvaluationService  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行产品智能诊断平台的评测集")
    parser.add_argument("--tenant", default="demo", help="租户 code（默认 demo）")
    parser.add_argument("--dataset-version", default="eval-1.0.0")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条")
    parser.add_argument(
        "--category",
        action="append",
        default=None,
        help="只跑指定类别（可重复），例如 --category conflict_evidence",
    )
    parser.add_argument("--name", default=None, help="评测运行名称")
    parser.add_argument("--show-failures", type=int, default=10, help="打印最多 N 条失败用例")
    parser.add_argument(
        "--allow-real-models",
        action="store_true",
        help="确认在已配置真实模型的情况下也照跑（会按用例数产生真实费用）",
    )
    args = parser.parse_args(argv)

    # 这个脚本读的是 backend/.env：如果那里配了真实模型，评测会**真的花钱**。
    # 踩过一次（以为在跑离线基线，实际按真实模型跑了 50 条）——所以这里必须显式确认。
    settings = get_settings()
    using_real_model = settings.llm_provider != "stub" and bool(settings.llm_api_key)
    if using_real_model and not args.allow_real_models:
        cases = args.limit or 50
        estimate = cases * 0.13  # 实测单条诊断约 ¥0.13（4 个分析 Agent + 报告 Agent）
        print("!! 当前配置会调用真实模型，评测将产生实际费用：")
        print(f"   provider={settings.llm_provider} model={settings.llm_model} 用例数≈{cases}")
        print(f"   预估费用 ≈ {estimate:.2f} {settings.currency}（按实测 ¥0.13/条估算）")
        print("   想要免费的离线基线，请先设置 LLM_PROVIDER=stub（或只跑 --limit 少量用例）。")
        print("   确认要花钱请加 --allow-real-models 重跑。")
        return 2

    session = SessionLocal()
    try:
        tenant = session.query(Tenant).filter(Tenant.code == args.tenant).first()
        if tenant is None:
            print(f"未找到租户 {args.tenant}，请先执行：python -m app.db.seed")
            return 1

        run = EvaluationService(session).run(
            tenant_id=tenant.id,
            dataset_version=args.dataset_version,
            name=args.name or f"评测 {args.dataset_version}",
            limit=args.limit,
            categories=args.category,
        )
        print(f"评测运行：{run.id}")
        print(f"数据集版本={run.dataset_version} 工作流={run.workflow_version} "
              f"Prompt={run.prompt_version} Embedding={run.embedding_version}")
        print(f"用例数={run.case_count} 状态={run.status} 平均耗时={run.avg_latency_ms}ms")
        print(json.dumps(run.metrics, ensure_ascii=False, indent=2))

        failed = [detail for detail in run.details if not detail["passed"]]
        if failed:
            print(f"\n未通过用例（最多显示 {args.show_failures} 条）：")
            for detail in failed[: args.show_failures]:
                print(f"  [{detail['category']}] {detail['question'][:50]}")
                print(
                    f"      任务状态={detail['task_status']} 错误码={detail['error_code']} "
                    f"冲突={detail['conflict_count']} 证据={detail['evidence_count']}"
                )
                print(f"      判定明细={detail['checks']}")
        print(f"\n通过 {len(run.details) - len(failed)}/{len(run.details)}")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
