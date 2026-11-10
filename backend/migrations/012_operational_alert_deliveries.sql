-- 运行告警外部投递 outbox：异步 Webhook、租约、指数退避与死信。

CREATE TABLE IF NOT EXISTS operational_alert_deliveries (
    id VARCHAR(36) PRIMARY KEY,
    alert_id VARCHAR(36) NOT NULL,
    tenant_id VARCHAR(36) NOT NULL,
    channel VARCHAR(24) NOT NULL,
    occurrence_no INTEGER NOT NULL,
    status VARCHAR(24) NOT NULL,
    payload JSON NOT NULL,
    attempts INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    visible_at TIMESTAMP WITHOUT TIME ZONE,
    claimed_by VARCHAR(128),
    claimed_at TIMESTAMP WITHOUT TIME ZONE,
    lease_expires_at TIMESTAMP WITHOUT TIME ZONE,
    last_error TEXT,
    last_status_code INTEGER,
    delivered_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT uq_operational_alert_delivery_occurrence
        UNIQUE (alert_id, occurrence_no, channel)
);

CREATE INDEX IF NOT EXISTS ix_operational_alert_deliveries_alert_id
    ON operational_alert_deliveries (alert_id);
CREATE INDEX IF NOT EXISTS ix_operational_alert_deliveries_tenant_id
    ON operational_alert_deliveries (tenant_id);
CREATE INDEX IF NOT EXISTS ix_operational_alert_deliveries_claimed_by
    ON operational_alert_deliveries (claimed_by);
CREATE INDEX IF NOT EXISTS ix_operational_alert_deliveries_lease_expires_at
    ON operational_alert_deliveries (lease_expires_at);
CREATE INDEX IF NOT EXISTS ix_alert_delivery_status_visible
    ON operational_alert_deliveries (status, visible_at);
CREATE INDEX IF NOT EXISTS ix_alert_delivery_tenant_status
    ON operational_alert_deliveries (tenant_id, status);
