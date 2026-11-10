-- 反思机制灰度发布状态：所有 API/worker 副本共享，支持暂停、回滚和人工批准扩大。

CREATE TABLE IF NOT EXISTS reflection_release_states (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id VARCHAR(36) NOT NULL,
    status VARCHAR(32) NOT NULL,
    current_percent INTEGER NOT NULL,
    recommended_percent INTEGER,
    recommendation VARCHAR(32) NOT NULL,
    reason TEXT,
    metrics JSON NOT NULL,
    version INTEGER NOT NULL,
    updated_by VARCHAR(36),
    last_evaluated_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT uq_reflection_release_tenant UNIQUE (tenant_id)
);

CREATE INDEX IF NOT EXISTS ix_reflection_release_status
    ON reflection_release_states (status);
CREATE INDEX IF NOT EXISTS ix_reflection_release_states_tenant_id
    ON reflection_release_states (tenant_id);
CREATE INDEX IF NOT EXISTS ix_reflection_release_states_updated_by
    ON reflection_release_states (updated_by);
