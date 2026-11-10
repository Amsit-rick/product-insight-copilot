-- 租户级常用回复模板。模板只生成草稿，发送仍经过工单回复/审批门禁。
CREATE TABLE IF NOT EXISTS ticket_reply_templates (
    id VARCHAR(36) PRIMARY KEY,
    tenant_id VARCHAR(36) NOT NULL,
    title VARCHAR(100) NOT NULL,
    content TEXT NOT NULL,
    issue_type VARCHAR(32),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_by VARCHAR(36),
    updated_by VARCHAR(36),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_reply_template_tenant_active
    ON ticket_reply_templates (tenant_id, active);
CREATE INDEX IF NOT EXISTS ix_reply_template_issue_type
    ON ticket_reply_templates (issue_type);
CREATE INDEX IF NOT EXISTS ix_ticket_reply_templates_tenant_id
    ON ticket_reply_templates (tenant_id);
