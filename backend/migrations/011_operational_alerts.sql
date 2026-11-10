-- 管理员运行告警中心：可确认、可解决；审计日志仍单独保留为不可变证据。

CREATE TABLE IF NOT EXISTS operational_alerts (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id VARCHAR(36) NOT NULL,
    kind VARCHAR(64) NOT NULL,
    severity VARCHAR(16) NOT NULL,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    status VARCHAR(24) NOT NULL,
    source_type VARCHAR(64),
    source_id VARCHAR(64),
    fingerprint VARCHAR(160) NOT NULL,
    occurrence_count INTEGER NOT NULL,
    first_occurred_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    last_occurred_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    acknowledged_by VARCHAR(36),
    acknowledged_at TIMESTAMP WITHOUT TIME ZONE,
    resolved_by VARCHAR(36),
    resolved_at TIMESTAMP WITHOUT TIME ZONE,
    metadata JSON NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT uq_operational_alert_fingerprint UNIQUE (tenant_id, fingerprint)
);

CREATE INDEX IF NOT EXISTS ix_operational_alerts_tenant_id
    ON operational_alerts (tenant_id);
CREATE INDEX IF NOT EXISTS ix_operational_alerts_kind
    ON operational_alerts (kind);
CREATE INDEX IF NOT EXISTS ix_operational_alerts_source_id
    ON operational_alerts (source_id);
CREATE INDEX IF NOT EXISTS ix_operational_alerts_acknowledged_by
    ON operational_alerts (acknowledged_by);
CREATE INDEX IF NOT EXISTS ix_operational_alerts_resolved_by
    ON operational_alerts (resolved_by);
CREATE INDEX IF NOT EXISTS ix_operational_alert_tenant_status
    ON operational_alerts (tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_operational_alert_severity
    ON operational_alerts (severity, last_occurred_at);
