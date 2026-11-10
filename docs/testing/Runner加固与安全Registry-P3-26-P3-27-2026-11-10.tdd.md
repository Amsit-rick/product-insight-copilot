# Runner 加固与安全 Registry P3-26 / P3-27（2026-11-10）

## 1. 验证目标与边界

本轮完成两个本地可验证目标：

1. P3-26：为供应链 Runner 增加单实例锁、共享截止时间、精确残留恢复和失败关闭验证；
2. P3-27：验证带一次性 CA、TLS 和 Basic Auth 的本地 OCI Registry，完成真实 push 与按 digest pull。

本轮没有企业 Gitee Go 主机组、企业 Registry、KMS/keyless、透明日志或独立目标主机。因此所有证据继续明确记录 `enterprise_registry_verified=false`、`cross_host_verified=false` 和 `production_ready=false`，不能作为生产交付证明。

## 2. P3-26 Runner 加固

### 2.1 工程改造

- Runner 准入新增 `timeout` 工具检查；
- 使用仓库 `.tmp` 下原子目录锁，锁冲突立即失败，内部退出码为 3；
- 锁路径必须位于仓库 `.tmp` 且以 `.lock` 结尾；
- `RELEASE_SHA` 在创建锁前必须是完整小写 Git SHA；
- 准入、镜像供应链、证据打包共用同一截止时间，后续阶段只能使用剩余时间预算；
- `EXIT`、`INT`、`TERM` 均自动释放当前进程持有的锁；
- 新增陈旧锁恢复器，默认 dry-run，只接受 `pic-*` 项目，最小锁龄 300 秒，PID 存活时拒绝清理；
- apply 模式只执行指定 Compose 项目的 `down -v --remove-orphans`，不使用 `docker system prune`。

### 2.2 负向与恢复验证

| 场景 | 结果 |
|---|---|
| 预创建 Runner 锁后再次启动 | 失败关闭，内部退出码 3，未进入镜像构建 |
| 陈旧锁 dry-run | 只输出计划，不删除锁、不执行 Compose 清理 |
| 陈旧锁 apply | 仅清理 `pic-p3-26-recovery` 并删除对应锁 |
| 全局 Docker prune | 未使用 |

### 2.3 完整 Linux Runner 演练

- Runner 镜像：按 digest 固定的 `docker:27-cli`；
- 提交：`089504ea2876cb8a956fa5998879d72b00ba1901`；
- Registry project：`pic-p3-26-runner`，回环端口 15010；
- 镜像 index digest：`sha256:fcd79c7da7b1d4890b01fc11ec83b163ace188c0c7f596e61008baad2e16e754`；
- provenance、SPDX 2.3 和 198 个包验证通过；
- Cosign 签名 1 个，`release_sha` 注解验证通过；
- bundle ID：`86923a7bda639e0db581d891ec5de90ea6a14ccc783afc8eaba6fa8a426598d8`；
- 退出后 Runner 锁、Registry 容器、网络和卷均已清理。

该演练仍使用 root 临时容器、一次性本地密钥、回环 Registry 和共享 Docker daemon，正确记录 `self_hosted_runner_verified=false`、`local_runner_rehearsal_verified=true`。

## 3. P3-27 TLS / Auth Registry

### 3.1 工程改造

- 使用 `cryptography` 生成一次性本地 CA 和服务证书；
- 服务证书 SAN 同时覆盖 `localhost` 和 `127.0.0.1`；
- 使用按 digest 固定的 `httpd` 镜像，通过 stdin 生成 bcrypt htpasswd，密码不进入命令行参数；
- Registry 只绑定 `127.0.0.1`，启用 TLS 和 Basic Auth；
- Registry 使用只读根文件系统、`cap_drop=ALL`、`no-new-privileges`、CPU/内存/PID 上限；
- 不修改 Docker Desktop 或操作系统信任库；
- 验证器直接通过 Registry HTTP API 上传 config、layer 和 OCI manifest，再按 manifest digest 拉取并复验内容哈希；
- 证书、私钥、htpasswd 和随机密码只存在于 `.tmp`，演练结束自动删除。

### 3.2 真实验证结果

| 检查 | 结果 |
|---|---|
| 未提供演练 CA | TLS 校验失败，符合预期 |
| 无凭据访问 | HTTP 401 |
| 错误凭据访问 | HTTP 401 |
| 正确 CA 与凭据 | HTTP 200 |
| blob / manifest push | PASS |
| 按 digest pull 与哈希复验 | PASS |
| 临时材料与 Compose 资源清理 | PASS |

首次运行发现 Windows PowerShell 5.1 不支持 `RandomNumberGenerator.GetBytes(32)` 静态重载。已改为实例式 CSPRNG，在保持 32 字节随机强度和密码不落日志的前提下兼容当前环境，修复后完整演练通过。

P3-27 绑定提交 `509d909ce21a52fcfc65cc102655e175dd387f28`，manifest digest 为 `sha256:2f77ded70af1c0625fbeae1f88bbe407b43d73b82ec2d1f7ad99ecde666f174c`。

## 4. 自动化验证

| 检查 | 结果 |
|---|---|
| P3-26 / P3-27 专项 | 39/39 PASS |
| 后端全量 | 1427 collected / 1426 passed / 1 skipped / 0 failed |
| Python 编译 | PASS |
| PowerShell AST 解析 | PASS |
| Bash 语法 | PASS |
| Secure Registry Compose 解析 | PASS |
| 敏感信息扫描 | PASS |
| `git diff --check` | PASS |

## 5. 量化进度

| 子项 | 完成度 |
|---|---|
| P3-26 本地 Runner 锁、全局截止时间与恢复 | 100% |
| P3-26 本地完整供应链回归 | 100% |
| P3-27 本地 TLS / Basic Auth Registry | 100% |
| P3-27 push 与 digest pull 复验 | 100% |
| 企业自托管 Runner 远程实跑 | 0% |
| 企业 Registry / KMS / 跨主机验证 | 0% |

本轮承诺的 P3-26 + P3-27 本地改造完成度为 **100%**。生产环境外部能力没有被本地演练替代，整体仍为 `production_ready=false`。

## 6. 结构化证据

- `docs/testing/artifacts/runner-readiness-p3-26-2026-11-10.json`；
- `docs/testing/artifacts/runner-image-attestations-p3-26-2026-11-10.json`；
- `docs/testing/artifacts/runner-image-signature-p3-26-2026-11-10.json`；
- `docs/testing/artifacts/runner-supply-chain-bundle-p3-26-2026-11-10.json`；
- `docs/testing/artifacts/runner-recovery-dry-run-p3-26-2026-11-10.json`；
- `docs/testing/artifacts/runner-recovery-apply-p3-26-2026-11-10.json`；
- `docs/testing/artifacts/secure-registry-material-p3-27-2026-11-10.json`；
- `docs/testing/artifacts/secure-registry-p3-27-2026-11-10.json`。
