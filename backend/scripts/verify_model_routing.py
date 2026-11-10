"""端到端验证多模型路由：跑一个真实任务，看每个 Agent 实际用的模型与降级行为。

用一个**不可达的端点 + 假密钥**来触发真实的"配置了模型但调用失败"路径，
以此证明：
1. 每个 Agent 解析到的是它自己配置的模型（跨供应商也能覆盖 base_url/api_key）；
2. 调用失败时不会伪造结论，而是如实降级到确定性归纳（model_used=false）；
3. agent_runs 记录的 model 是"该 Agent 配置的模型"，便于审计。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

# 必须在导入 app.* 之前设置环境变量
os.environ["LLM_PROVIDER"] = "openai_compatible"
os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"  # 不可达，模拟模型服务故障
os.environ["LLM_API_KEY"] = "sk-global-fake"
os.environ["LLM_MODEL"] = "deepseek-chat"
os.environ["LLM_MAX_RETRIES"] = "0"
os.environ["AGENT_MODEL_OVERRIDES"] = json.dumps(
    {
        "report_agent": {"model": "deepseek-reasoner"},
        "feedback_agent": {
            "model": "qwen-max",
            "base_url": "http://127.0.0.1:9/dashscope/v1",
            "api_key": "sk-qwen-fake",
        },
        "intent_classifier": {"model": "qwen-turbo"},
    }
)

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.db.session import SessionLocal  # noqa: E402
from app.models.identity import User  # noqa: E402
from app.models.workflow import AgentRun, AnalysisTask  # noqa: E402
from app.services.model_gateway import get_model_gateway  # noqa: E402
from app.services.task_service import TaskService  # noqa: E402
from app.services.workflow_runner import WorkflowRunner  # noqa: E402

QUERY = (
    "Android 端 3.2.0 发布后次日留存下降，哪些用户受到影响？"
    "版本变更、用户反馈和故障记录是否支持版本相关性？"
)
FILTERS = {
    "platform": "Android",
    "app_version": "3.2.0",
    "date_from": "2026-08-01",
    "date_to": "2026-08-31",
    "knowledge_bases": ["product_kb", "feedback_kb", "engineering_kb", "operation_kb"],
    "metrics": ["next_day_retention", "crash_rate"],
}


def main() -> int:
    settings = get_settings()
    gateway = get_model_gateway()

    print("=== 路由解析结果（不含密钥）===")
    print(f"  覆盖配置条目数: {len(settings.agent_model_overrides)}")
    for item in gateway.routing_table():
        print(
            f"  {item['key']:15} model={item['model']:20} source={item['source']:9} "
            f"endpoint={item['base_url']}"
        )
    for key in ("intent_classifier", "answerability_check"):
        item = gateway.profile_for(key).describe()
        print(f"  {key:15} model={item['model']:20} source={item['source']:9} endpoint={item['base_url']}")

    session = SessionLocal()
    user = session.query(User).filter(User.username == "pm").first()
    task, _ = TaskService(session).create(user=user, query=QUERY, filters=FILTERS)
    session.commit()
    task_id = task.id
    session.close()
    print(f"\n任务已创建: {task_id}")

    result = WorkflowRunner().run(task_id)
    print(f"执行结果: {json.dumps(result, ensure_ascii=False, default=str)[:200]}")

    session = SessionLocal()
    task = session.get(AnalysisTask, task_id)
    print(f"任务状态: {task.status}（模型全部不可达，系统仍应产出报告）")
    print("\n=== agent_runs 实际记录 ===")
    runs = session.query(AgentRun).filter(AgentRun.task_id == task_id).order_by(AgentRun.created_at).all()
    for run in runs:
        output = run.output or {}
        print(
            f"  {run.agent_code:15} model={run.model:20} model_used={output.get('model_used')} "
            f"status={run.status} 结论={len(output.get('findings') or [])} 条 错误={str(run.error)[:40]}"
        )
    session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
