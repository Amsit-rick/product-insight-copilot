-- 任务级确定性历史播放：完整脱敏工具结果 + 不可变回放快照。

ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS result JSON;

ALTER TABLE tool_calls
    ADD COLUMN IF NOT EXISTS result_digest VARCHAR(64);

CREATE TABLE IF NOT EXISTS task_replay_snapshots (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id VARCHAR(36) NOT NULL,
    task_id VARCHAR(36) NOT NULL,
    schema_version VARCHAR(64) NOT NULL,
    package JSON NOT NULL,
    digest VARCHAR(64) NOT NULL,
    created_by VARCHAR(36),
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT uq_replay_task_digest UNIQUE (task_id, digest)
);

CREATE INDEX IF NOT EXISTS ix_replay_task_created
    ON task_replay_snapshots (task_id, created_at);
CREATE INDEX IF NOT EXISTS ix_task_replay_snapshots_created_by
    ON task_replay_snapshots (created_by);
CREATE INDEX IF NOT EXISTS ix_task_replay_snapshots_digest
    ON task_replay_snapshots (digest);
CREATE INDEX IF NOT EXISTS ix_task_replay_snapshots_task_id
    ON task_replay_snapshots (task_id);
CREATE INDEX IF NOT EXISTS ix_task_replay_snapshots_tenant_id
    ON task_replay_snapshots (tenant_id);
