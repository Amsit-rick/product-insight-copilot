-- =============================================================================
-- 电商售后工单智能处理系统 -- 工单域迁移（003_tickets）
-- =============================================================================
--
-- 【适用环境】PostgreSQL。执行：python -m app.db.migrate apply
--
-- 【为什么两条路径都要有】
--   - 新库：001_init.sql 已包含这些表，本文件用 IF NOT EXISTS 保证重复执行为空操作；
--   - 存量库（已跑过旧版 001）：执行本文件即可补齐，不需要重建数据库。
--
-- 【本地 / 测试环境】SQLite 下请用 python -m app.db.seed（按模型建表），不要执行本文件。
--
-- 【与代码的对应关系】本文件由 backend/.tmp/gen_ticket_ddl.py 从模型编译生成，
--   修改模型后请重跑该脚本，不要手改。
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 电商售后工单（需求书 §5 数据模型）
-- 新增于阶段改造：工单 / 消息 / 订单事实 / 处理建议 / 政策生效期
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tickets (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	external_id VARCHAR(64), 
	customer_id VARCHAR(64), 
	order_id VARCHAR(64), 
	shop_id VARCHAR(64), 
	platform VARCHAR(32), 
	subject VARCHAR(255) NOT NULL, 
	content TEXT NOT NULL, 
	channel VARCHAR(32) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	issue_type VARCHAR(32), 
	priority VARCHAR(16) NOT NULL, 
	sentiment VARCHAR(16), 
	risk_tier VARCHAR(4), 
	assignee_id VARCHAR(36), 
	created_by VARCHAR(36), 
	last_analysis_task_id VARCHAR(36), 
	analysis_count INTEGER NOT NULL, 
	attributes JSON NOT NULL, 
	escalation_reason VARCHAR(255), 
	missing_fields JSON NOT NULL, 
	quality_status VARCHAR(24) NOT NULL,
	quality_issue_codes JSON NOT NULL,
	quality_reviewed_at TIMESTAMP WITHOUT TIME ZONE,
	quality_reviewer_id VARCHAR(36),
	idempotency_key VARCHAR(128), 
	closed_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_ticket_tenant_created ON tickets (tenant_id, created_at);
CREATE INDEX IF NOT EXISTS ix_ticket_tenant_status ON tickets (tenant_id, status);
CREATE INDEX IF NOT EXISTS ix_tickets_assignee_id ON tickets (assignee_id);
CREATE INDEX IF NOT EXISTS ix_tickets_customer_id ON tickets (customer_id);
CREATE INDEX IF NOT EXISTS ix_tickets_external_id ON tickets (external_id);
CREATE INDEX IF NOT EXISTS ix_tickets_idempotency_key ON tickets (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_tickets_issue_type ON tickets (issue_type);
CREATE INDEX IF NOT EXISTS ix_tickets_last_analysis_task_id ON tickets (last_analysis_task_id);
CREATE INDEX IF NOT EXISTS ix_tickets_order_id ON tickets (order_id);
CREATE INDEX IF NOT EXISTS ix_tickets_platform ON tickets (platform);
CREATE INDEX IF NOT EXISTS ix_tickets_quality_status ON tickets (quality_status);
CREATE INDEX IF NOT EXISTS ix_tickets_shop_id ON tickets (shop_id);
CREATE INDEX IF NOT EXISTS ix_tickets_status ON tickets (status);
CREATE INDEX IF NOT EXISTS ix_tickets_tenant_id ON tickets (tenant_id);
CREATE TABLE IF NOT EXISTS ticket_messages (
	id VARCHAR(36) NOT NULL, 
	ticket_id VARCHAR(36) NOT NULL, 
	sender_type VARCHAR(16) NOT NULL, 
	actor_id VARCHAR(36), 
	content TEXT NOT NULL, 
	attachments JSON NOT NULL, 
	visible_to_customer BOOLEAN NOT NULL, 
	meta JSON NOT NULL, 
	idempotency_key VARCHAR(128),
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id),
	CONSTRAINT uq_ticket_message_idempotency UNIQUE (ticket_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS ix_message_ticket_created ON ticket_messages (ticket_id, created_at);
CREATE INDEX IF NOT EXISTS ix_ticket_messages_ticket_id ON ticket_messages (ticket_id);
CREATE INDEX IF NOT EXISTS ix_ticket_messages_idempotency_key ON ticket_messages (idempotency_key);
CREATE TABLE IF NOT EXISTS order_facts (
	id VARCHAR(36) NOT NULL, 
	ticket_id VARCHAR(36) NOT NULL, 
	source VARCHAR(16) NOT NULL, 
	fact_type VARCHAR(32) NOT NULL, 
	payload JSON NOT NULL, 
	observed_at TIMESTAMP WITHOUT TIME ZONE, 
	request_id VARCHAR(64), 
	is_mock BOOLEAN NOT NULL, 
	adapter VARCHAR(32), 
	error JSON, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id),
	CONSTRAINT uq_reco_ticket_analysis_task UNIQUE (ticket_id, analysis_task_id)
);
CREATE INDEX IF NOT EXISTS ix_fact_ticket_source ON order_facts (ticket_id, source);
CREATE INDEX IF NOT EXISTS ix_order_facts_request_id ON order_facts (request_id);
CREATE INDEX IF NOT EXISTS ix_order_facts_ticket_id ON order_facts (ticket_id);
CREATE TABLE IF NOT EXISTS recommendations (
	id VARCHAR(36) NOT NULL, 
	ticket_id VARCHAR(36) NOT NULL, 
	analysis_task_id VARCHAR(36), 
	action_type VARCHAR(32) NOT NULL, 
	amount NUMERIC(12, 2), 
	amount_source VARCHAR(64),
	currency VARCHAR(8) NOT NULL, 
	rationale TEXT, 
	evidence_ids JSON NOT NULL, 
	policy_refs JSON NOT NULL, 
	missing_information JSON NOT NULL, 
	risk_tier VARCHAR(4) NOT NULL, 
	risk_reason VARCHAR(255), 
	approval_requirement VARCHAR(16) NOT NULL, 
	confidence FLOAT, 
	status VARCHAR(24) NOT NULL, 
	customer_reply TEXT, 
	internal_note TEXT, 
	warnings JSON NOT NULL, 
	decided_by VARCHAR(36), 
	decided_at TIMESTAMP WITHOUT TIME ZONE, 
	decision_reason TEXT, 
	original_reply TEXT, 
	sent_at TIMESTAMP WITHOUT TIME ZONE, 
	executed_at TIMESTAMP WITHOUT TIME ZONE, 
	idempotency_key VARCHAR(128), 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_reco_analysis_task ON recommendations (analysis_task_id);
CREATE INDEX IF NOT EXISTS ix_reco_ticket_status ON recommendations (ticket_id, status);
CREATE INDEX IF NOT EXISTS ix_recommendations_idempotency_key ON recommendations (idempotency_key);
CREATE INDEX IF NOT EXISTS ix_recommendations_risk_tier ON recommendations (risk_tier);
CREATE INDEX IF NOT EXISTS ix_recommendations_status ON recommendations (status);
CREATE INDEX IF NOT EXISTS ix_recommendations_ticket_id ON recommendations (ticket_id);
CREATE TABLE IF NOT EXISTS policy_documents (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	title VARCHAR(255) NOT NULL, 
	version VARCHAR(32) NOT NULL, 
	scope JSON NOT NULL, 
	effective_from TIMESTAMP WITHOUT TIME ZONE NOT NULL, 
	effective_to TIMESTAMP WITHOUT TIME ZONE, 
	status VARCHAR(16) NOT NULL, 
	source_uri VARCHAR(512), 
	document_id VARCHAR(36), 
	kb_code VARCHAR(32), 
	note TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS ix_policy_documents_document_id ON policy_documents (document_id);
CREATE INDEX IF NOT EXISTS ix_policy_documents_tenant_id ON policy_documents (tenant_id);
CREATE INDEX IF NOT EXISTS ix_policy_effective ON policy_documents (effective_from, effective_to);
CREATE INDEX IF NOT EXISTS ix_policy_tenant_scope ON policy_documents (tenant_id, status);
