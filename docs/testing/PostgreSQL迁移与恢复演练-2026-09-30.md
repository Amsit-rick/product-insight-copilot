# PostgreSQL 迁移与恢复演练（2026-09-30）

## 结论

本地一次性 PostgreSQL 16 + pgvector 环境已完成“全新库迁移 → 写入探针 → 快照 → 模拟破坏 → 隔离库恢复 → 数据核对”，演练通过。该结果证明仓库当前迁移和快照恢复流程可执行，不代表生产备份策略、RPO/RTO 或生产基础设施已经验收。

## 首次红灯与修复

- 首次对全新 PostgreSQL 执行 001–006 时，`004_amount_source.sql` 报 `DuplicateColumn`。
- 根因：`001_init.sql` 已包含 `recommendations.amount_source`，004 又无条件添加同名列。
- 修复：004 改为 `ADD COLUMN IF NOT EXISTS`，并增加单测，禁止增量迁移再次引入无幂等的 `ADD COLUMN`。

## 实际演练

执行入口：

```cmd
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\rehearse-postgres-restore.ps1 -PythonPath backend\.tmp\clean-venv-20260930\Scripts\python.exe
```

脚本只操作自己创建的一次性容器，并在结束时删除容器。实际步骤：

1. 启动 `pgvector/pgvector:pg16` 一次性容器。
2. 在源库执行 001–006 共 6 个迁移。
3. 写入 `restore_probe=before-backup`，使用 `pg_dump -Fc` 生成快照。
4. 删除 `tickets` 表和探针数据，模拟不可逆破坏。
5. 将快照恢复到新的 `pic_restore` 隔离库，不覆盖源库。
6. 核对 `schema_migrations=6`、`tickets` 表存在、探针数据完整。

实测结果：**通过**。迁移专项单测：**19/19 通过**。

## 交付边界

- 选择快照恢复而非自动 downgrade，是因为删除新增列/表会永久丢数据；数据层回滚必须先停写，并先恢复到隔离库核对。
- 正式发布前仍需由运维在目标环境执行一次同等演练，确定快照保存位置、权限、加密、保留期以及业务允许的 RPO/RTO。
