# Saleor 业务场景 Slice 3 TDD 证据

日期：2026-11-15
范围：本地高保真 Saleor Sandbox 的十类确定性业务场景
证据口径：`local_high_fidelity_sandbox`，不是企业生产证据

## RED

首次运行：

```cmd
cd /d D:\path\to\product-insight-copilot\backend
..\.venv\Scripts\python.exe -m pytest tests\unit\test_sandbox_scenarios.py -q
```

结果：测试收集失败，`ScenarioFactProvider` 不存在。

新增容器交付契约后运行：

```cmd
..\.venv\Scripts\python.exe -m pytest tests\unit\test_commerce_sandbox_delivery.py -q
```

结果：2 项失败，缺少 `commerce-scenarios` 服务和 `build_saleor_scenarios.py`。

## GREEN

相关回归：

```cmd
..\.venv\Scripts\python.exe -m pytest tests\unit\test_reference_integration_api.py tests\unit\test_saleor_fact_provider.py tests\unit\test_commerce_sandbox_delivery.py tests\unit\test_reference_integration_sandbox_delivery.py tests\unit\test_sandbox_scenarios.py tests\integration\test_fact_http_reference.py -q
```

结果：38 通过、0 失败。

覆盖率：

```cmd
..\.venv\Scripts\python.exe -m pytest tests\unit\test_sandbox_scenarios.py tests\unit\test_saleor_fact_provider.py tests\unit\test_reference_integration_api.py --cov=sandbox_services.scenarios --cov=sandbox_services.providers --cov=sandbox_services.app --cov-report=term-missing -q
```

结果：24 通过；场景模块 86%，三个 Sandbox 模块合计 85%。

## Docker 与真实 HTTP 验收

```cmd
cd /d D:\path\to\product-insight-copilot
powershell -ExecutionPolicy Bypass -File scripts\commerce_sandbox.ps1 start
cd backend
..\.venv\Scripts\python.exe scripts\verify_saleor_scenarios.py --base-url http://127.0.0.1:8092 --json-out ..\docs\testing\artifacts\saleor-commerce-sandbox-slice3-local.json
```

结果：

- 43/43 检查通过；
- 十个场景均存在且公开字段集合固定；
- 十场景 × 四类事实 = 40 次查询全部成功；
- 来源版本固定为 `saleor-3.23.49+scenario-v1`；
- P50 61.30ms，P95 87.59ms；
- 第二次启动未重建清单，日志显示已有十个场景。

## 安全与真实性边界

- 场景清单不包含地址、手机号、邮箱、客户号、Token、密码或授权头。
- `GET /scenarios` 不返回上游订单映射或覆盖层内容。
- 本轮数据由 Saleor 本地合成订单与显式 local overlay 组成。
- `enterprise_endpoint_verified=false`。
- `real_payment_verified=false`。
- `real_carrier_verified=false`。
- `production_ready=false`。
