-- =============================================================================
-- 产品智能诊断平台 / Product Insight Copilot -- 初始化迁移（001_init）
-- =============================================================================
--
-- 【生成方式】本文件由 SQLAlchemy 模型元数据自动生成，请勿手工编辑。
--   from sqlalchemy.schema import CreateTable, CreateIndex
--   from sqlalchemy.dialects import postgresql
--   for t in Base.metadata.sorted_tables:
--       CreateTable(t).compile(dialect=postgresql.dialect())
--       for ix in t.indexes: CreateIndex(ix).compile(dialect=postgresql.dialect())
--   模型（backend/app/models/*.py）是 schema 的唯一事实来源；模型改动后请重新生成，
--   并运行 pytest tests/unit/test_migrate.py 校验表名/表数量与模型一致。
--
-- 【适用环境】PostgreSQL（deploy/docker-compose.yml 中的 pgvector/pgvector:pg16）。
--   执行：python -m app.db.migrate apply
--
-- 【本地 / 测试环境】默认使用 SQLite，**不走本文件**（本文件是 Postgres DDL），
--   请改用：python -m app.db.seed    （Base.metadata.create_all + 演示数据播种）
--
-- 【说明】
-- - 主键为 String(36) 的 UUID 字符串，由应用层生成（app.models.base.new_id），
--   数据库侧没有 gen_random_uuid() 之类默认值，因此**不需要 pgcrypto 扩展**。
-- - JSON 列按模型定义（sqlalchemy.types.JSON）原样编译为 JSON；
--   未手工改写为 JSONB，保证生成结果与 create_all 的语义完全一致。
-- - 向量表 chunk_vectors 依赖 pgvector 扩展，单独放在 002_pgvector.sql。
-- =============================================================================


CREATE TABLE access_policies (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	subject_type VARCHAR(16) NOT NULL, 
	subject_id VARCHAR(64) NOT NULL, 
	resource_type VARCHAR(32) NOT NULL, 
	resource_id VARCHAR(64) NOT NULL, 
	effect VARCHAR(8) NOT NULL, 
	actions JSON NOT NULL, 
	conditions JSON NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_access_policies_resource_id ON access_policies (resource_id);
CREATE INDEX ix_access_policies_subject_id ON access_policies (subject_id);
CREATE INDEX ix_access_policies_tenant_id ON access_policies (tenant_id);


CREATE TABLE agent_runs (
	id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36) NOT NULL, 
	agent_code VARCHAR(64) NOT NULL, 
	node VARCHAR(64), 
	status VARCHAR(32) NOT NULL, 
	attempt INTEGER NOT NULL, 
	model VARCHAR(64), 
	prompt_version VARCHAR(32), 
	input_summary TEXT, 
	output JSON, 
	latency_ms INTEGER, 
	prompt_tokens INTEGER, 
	completion_tokens INTEGER, 
	total_tokens INTEGER, 
	error TEXT, 
	started_at TIMESTAMP WITHOUT TIME ZONE, 
	finished_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_agent_run_task ON agent_runs (task_id, agent_code);
CREATE INDEX ix_agent_runs_task_id ON agent_runs (task_id);


CREATE TABLE analysis_events (
	id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36) NOT NULL, 
	node VARCHAR(64), 
	level VARCHAR(16) NOT NULL, 
	message TEXT NOT NULL, 
	payload JSON NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_analysis_events_task_id ON analysis_events (task_id);
CREATE INDEX ix_event_task ON analysis_events (task_id, created_at);


CREATE TABLE task_queue_items (
	id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	payload JSON NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	priority INTEGER NOT NULL, 
	attempts INTEGER NOT NULL, 
	max_attempts INTEGER NOT NULL, 
	visible_at TIMESTAMP WITHOUT TIME ZONE, 
	claimed_by VARCHAR(128), 
	claimed_at TIMESTAMP WITHOUT TIME ZONE, 
	lease_expires_at TIMESTAMP WITHOUT TIME ZONE, 
	last_error TEXT, 
	finished_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_queue_status_visible ON task_queue_items (status, visible_at);
CREATE INDEX ix_queue_task_kind ON task_queue_items (task_id, kind);
CREATE INDEX ix_task_queue_items_status ON task_queue_items (status);
CREATE INDEX ix_task_queue_items_task_id ON task_queue_items (task_id);

CREATE TABLE scheduler_leases (
	name VARCHAR(64) NOT NULL,
	owner_id VARCHAR(128),
	lease_expires_at TIMESTAMP WITHOUT TIME ZONE,
	heartbeat_at TIMESTAMP WITHOUT TIME ZONE,
	last_run_started_at TIMESTAMP WITHOUT TIME ZONE,
	last_run_finished_at TIMESTAMP WITHOUT TIME ZONE,
	last_status VARCHAR(32),
	last_error TEXT,
	last_result JSON NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (name)
);
CREATE INDEX ix_scheduler_leases_owner_id ON scheduler_leases (owner_id);
CREATE INDEX ix_scheduler_leases_lease_expires_at ON scheduler_leases (lease_expires_at);

CREATE TABLE analysis_tasks (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	user_id VARCHAR(36) NOT NULL, 
	title VARCHAR(255), 
	query TEXT NOT NULL, 
	filters JSON NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	current_node VARCHAR(64), 
	current_phase VARCHAR(64),
	checkpoint_version INTEGER NOT NULL,
	phase_attempt INTEGER NOT NULL,
	resume_count INTEGER NOT NULL,
	resumed_from_checkpoint BOOLEAN NOT NULL,
	wake_reason VARCHAR(64),
	state_revision INTEGER NOT NULL,
	intent VARCHAR(64), 
	plan JSON NOT NULL, 
	risk_level VARCHAR(16) NOT NULL, 
	retry_count INTEGER NOT NULL, 
	workflow_version VARCHAR(32), 
	prompt_version VARCHAR(32), 
	idempotency_key VARCHAR(128), 
	result JSON, 
	error JSON, 
	cancel_requested BOOLEAN NOT NULL, 
	waiting_reason VARCHAR(255), 
	started_at TIMESTAMP WITHOUT TIME ZONE, 
	finished_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_analysis_tasks_idempotency_key ON analysis_tasks (idempotency_key);
CREATE INDEX ix_analysis_tasks_status ON analysis_tasks (status);
CREATE INDEX ix_analysis_tasks_tenant_id ON analysis_tasks (tenant_id);
CREATE INDEX ix_analysis_tasks_user_id ON analysis_tasks (user_id);
CREATE INDEX ix_task_tenant_status ON analysis_tasks (tenant_id, status);


CREATE TABLE task_execution_units (
	id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	parent_unit_id VARCHAR(36),
	name VARCHAR(255) NOT NULL,
	unit_type VARCHAR(24) NOT NULL,
	owner_type VARCHAR(24) NOT NULL,
	owner_ref_id VARCHAR(64) NOT NULL,
	status VARCHAR(24) NOT NULL,
	execution_mode VARCHAR(16) NOT NULL,
	cancellation_policy VARCHAR(16) NOT NULL,
	failure_policy VARCHAR(16) NOT NULL,
	metadata JSON NOT NULL,
	started_at TIMESTAMP WITHOUT TIME ZONE,
	finished_at TIMESTAMP WITHOUT TIME ZONE,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_task_execution_owner UNIQUE (task_id, owner_type, owner_ref_id)
);
CREATE INDEX ix_task_execution_parent ON task_execution_units (task_id, parent_unit_id);
CREATE INDEX ix_task_execution_status ON task_execution_units (task_id, status);
CREATE INDEX ix_task_execution_units_owner_ref_id ON task_execution_units (owner_ref_id);
CREATE INDEX ix_task_execution_units_owner_type ON task_execution_units (owner_type);
CREATE INDEX ix_task_execution_units_parent_unit_id ON task_execution_units (parent_unit_id);
CREATE INDEX ix_task_execution_units_task_id ON task_execution_units (task_id);
CREATE INDEX ix_task_execution_units_unit_type ON task_execution_units (unit_type);


CREATE TABLE approvals (
	id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36) NOT NULL, 
	report_id VARCHAR(36), 
	status VARCHAR(32) NOT NULL, 
	action VARCHAR(32), 
	reviewer_id VARCHAR(36), 
	comment TEXT, 
	diff JSON, 
	decided_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_approval_task ON approvals (task_id);
CREATE INDEX ix_approvals_report_id ON approvals (report_id);
CREATE INDEX ix_approvals_task_id ON approvals (task_id);


CREATE TABLE audit_logs (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	actor_id VARCHAR(36), 
	actor_name VARCHAR(64), 
	action VARCHAR(64) NOT NULL, 
	resource_type VARCHAR(64), 
	resource_id VARCHAR(64), 
	outcome VARCHAR(16) NOT NULL, 
	detail JSON NOT NULL, 
	ip VARCHAR(64), 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_audit_logs_action ON audit_logs (action);
CREATE INDEX ix_audit_logs_actor_id ON audit_logs (actor_id);
CREATE INDEX ix_audit_logs_tenant_id ON audit_logs (tenant_id);
CREATE INDEX ix_audit_tenant ON audit_logs (tenant_id, created_at);


CREATE TABLE reflection_release_states (
	id VARCHAR(36) NOT NULL,
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
	PRIMARY KEY (id),
	CONSTRAINT uq_reflection_release_tenant UNIQUE (tenant_id)
);
CREATE INDEX ix_reflection_release_status ON reflection_release_states (status);
CREATE INDEX ix_reflection_release_states_tenant_id ON reflection_release_states (tenant_id);
CREATE INDEX ix_reflection_release_states_updated_by ON reflection_release_states (updated_by);


CREATE TABLE document_chunks (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	document_id VARCHAR(36) NOT NULL, 
	kb_id VARCHAR(36) NOT NULL, 
	kb_code VARCHAR(64) NOT NULL, 
	document_version VARCHAR(32) NOT NULL, 
	chunk_index INTEGER NOT NULL, 
	content TEXT NOT NULL, 
	token_count INTEGER NOT NULL, 
	page INTEGER, 
	section_title VARCHAR(255), 
	chunk_type VARCHAR(32) NOT NULL, 
	source_name VARCHAR(255), 
	department VARCHAR(64), 
	security_level VARCHAR(32) NOT NULL, 
	app_version VARCHAR(32), 
	doc_updated_at DATE, 
	embedding JSON, 
	embedding_version VARCHAR(32), 
	meta JSON NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_chunk_doc ON document_chunks (document_id);
CREATE INDEX ix_chunk_kb_tenant ON document_chunks (tenant_id, kb_code);
CREATE INDEX ix_document_chunks_app_version ON document_chunks (app_version);
CREATE INDEX ix_document_chunks_document_id ON document_chunks (document_id);
CREATE INDEX ix_document_chunks_kb_code ON document_chunks (kb_code);
CREATE INDEX ix_document_chunks_kb_id ON document_chunks (kb_id);
CREATE INDEX ix_document_chunks_security_level ON document_chunks (security_level);
CREATE INDEX ix_document_chunks_tenant_id ON document_chunks (tenant_id);


CREATE TABLE evaluation_cases (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	category VARCHAR(64) NOT NULL, 
	question TEXT NOT NULL, 
	filters JSON NOT NULL, 
	expected_kb VARCHAR(64), 
	expected_doc_ids JSON NOT NULL, 
	expected_intent VARCHAR(64), 
	expected_metric JSON, 
	expect_refusal BOOLEAN NOT NULL, 
	expect_conflict BOOLEAN NOT NULL, 
	expect_human_review BOOLEAN NOT NULL, 
	security_level VARCHAR(32) NOT NULL, 
	notes TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_evaluation_cases_category ON evaluation_cases (category);
CREATE INDEX ix_evaluation_cases_tenant_id ON evaluation_cases (tenant_id);


CREATE TABLE evaluation_runs (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	dataset_version VARCHAR(64) NOT NULL, 
	workflow_version VARCHAR(32), 
	prompt_version VARCHAR(32), 
	embedding_version VARCHAR(32), 
	case_count INTEGER NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	metrics JSON NOT NULL, 
	details JSON NOT NULL, 
	avg_latency_ms FLOAT, 
	started_at TIMESTAMP WITHOUT TIME ZONE, 
	finished_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_evaluation_runs_tenant_id ON evaluation_runs (tenant_id);


CREATE TABLE evidence_citations (
	id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36) NOT NULL, 
	report_id VARCHAR(36), 
	statement_id VARCHAR(64), 
	statement_type VARCHAR(32) NOT NULL, 
	source_kind VARCHAR(16) NOT NULL, 
	chunk_id VARCHAR(36), 
	document_id VARCHAR(36), 
	kb_code VARCHAR(64), 
	source_name VARCHAR(255), 
	page INTEGER, 
	document_version VARCHAR(32), 
	content TEXT, 
	relevance_score FLOAT, 
	metric_ref JSON, 
	permission_checked BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_citation_task ON evidence_citations (task_id);
CREATE INDEX ix_evidence_citations_report_id ON evidence_citations (report_id);
CREATE INDEX ix_evidence_citations_statement_type ON evidence_citations (statement_type);
CREATE INDEX ix_evidence_citations_task_id ON evidence_citations (task_id);


CREATE TABLE knowledge_bases (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	code VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	description TEXT, 
	owner VARCHAR(64), 
	security_level VARCHAR(32) NOT NULL, 
	document_count INTEGER NOT NULL, 
	chunk_count INTEGER NOT NULL, 
	index_status VARCHAR(32) NOT NULL, 
	embedding_version VARCHAR(32), 
	index_version VARCHAR(32), 
	last_indexed_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_kb_tenant_code UNIQUE (tenant_id, code)
);
CREATE INDEX ix_knowledge_bases_code ON knowledge_bases (code);
CREATE INDEX ix_knowledge_bases_security_level ON knowledge_bases (security_level);
CREATE INDEX ix_knowledge_bases_tenant_id ON knowledge_bases (tenant_id);


CREATE TABLE metric_definitions (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	name VARCHAR(64) NOT NULL, 
	display_name VARCHAR(128) NOT NULL, 
	description TEXT, 
	numerator VARCHAR(255), 
	denominator VARCHAR(255), 
	unit VARCHAR(16) NOT NULL, 
	period VARCHAR(16) NOT NULL, 
	allowed_dimensions JSON NOT NULL, 
	value_type VARCHAR(16) NOT NULL, 
	higher_is_better BOOLEAN NOT NULL, 
	source_version VARCHAR(64), 
	is_active BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_metric_definitions_name ON metric_definitions (name);
CREATE INDEX ix_metric_definitions_tenant_id ON metric_definitions (tenant_id);


CREATE TABLE metric_imports (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	file_name VARCHAR(255) NOT NULL, 
	dataset_version VARCHAR(64) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	rows_total INTEGER NOT NULL, 
	rows_valid INTEGER NOT NULL, 
	rows_invalid INTEGER NOT NULL, 
	report JSON NOT NULL, 
	created_by VARCHAR(36), 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_metric_imports_dataset_version ON metric_imports (dataset_version);
CREATE INDEX ix_metric_imports_tenant_id ON metric_imports (tenant_id);


CREATE TABLE metric_snapshots (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	metric_name VARCHAR(64) NOT NULL, 
	platform VARCHAR(32), 
	app_version VARCHAR(32), 
	channel VARCHAR(64), 
	device VARCHAR(64), 
	segment JSON NOT NULL, 
	stat_date DATE NOT NULL, 
	value FLOAT NOT NULL, 
	sample_size INTEGER, 
	source_version VARCHAR(64), 
	dataset_version VARCHAR(64), 
	quality_flags JSON NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_metric_lookup ON metric_snapshots (tenant_id, metric_name, stat_date);
CREATE INDEX ix_metric_snapshots_app_version ON metric_snapshots (app_version);
CREATE INDEX ix_metric_snapshots_channel ON metric_snapshots (channel);
CREATE INDEX ix_metric_snapshots_dataset_version ON metric_snapshots (dataset_version);
CREATE INDEX ix_metric_snapshots_device ON metric_snapshots (device);
CREATE INDEX ix_metric_snapshots_metric_name ON metric_snapshots (metric_name);
CREATE INDEX ix_metric_snapshots_platform ON metric_snapshots (platform);
CREATE INDEX ix_metric_snapshots_stat_date ON metric_snapshots (stat_date);
CREATE INDEX ix_metric_snapshots_tenant_id ON metric_snapshots (tenant_id);
CREATE INDEX ix_metric_version ON metric_snapshots (tenant_id, app_version);


CREATE TABLE reports (
	id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	version INTEGER NOT NULL, 
	summary TEXT, 
	impact_scope JSON NOT NULL, 
	facts JSON NOT NULL, 
	inferences JSON NOT NULL, 
	hypotheses JSON NOT NULL, 
	evidence JSON NOT NULL, 
	conflicts JSON NOT NULL, 
	recommendations JSON NOT NULL, 
	priority VARCHAR(8), 
	confidence FLOAT, 
	needs_human_review BOOLEAN NOT NULL, 
	risk_level VARCHAR(16) NOT NULL, 
	approval_status VARCHAR(32) NOT NULL, 
	content_json JSON NOT NULL, 
	content_md TEXT, 
	created_by VARCHAR(36), 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_reports_approval_status ON reports (approval_status);
CREATE INDEX ix_reports_task_id ON reports (task_id);
CREATE INDEX ix_reports_tenant_id ON reports (tenant_id);


CREATE TABLE roles (
	id VARCHAR(36) NOT NULL, 
	code VARCHAR(64) NOT NULL, 
	name VARCHAR(64) NOT NULL, 
	description TEXT, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_roles_code ON roles (code);


CREATE TABLE tenants (
	id VARCHAR(36) NOT NULL, 
	code VARCHAR(64) NOT NULL, 
	name VARCHAR(128) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE UNIQUE INDEX ix_tenants_code ON tenants (code);


CREATE TABLE task_replay_snapshots (
	id VARCHAR(36) NOT NULL,
	tenant_id VARCHAR(36) NOT NULL,
	task_id VARCHAR(36) NOT NULL,
	schema_version VARCHAR(64) NOT NULL,
	package JSON NOT NULL,
	digest VARCHAR(64) NOT NULL,
	created_by VARCHAR(36),
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_replay_task_digest UNIQUE (task_id, digest)
);
CREATE INDEX ix_replay_task_created ON task_replay_snapshots (task_id, created_at);
CREATE INDEX ix_task_replay_snapshots_created_by ON task_replay_snapshots (created_by);
CREATE INDEX ix_task_replay_snapshots_digest ON task_replay_snapshots (digest);
CREATE INDEX ix_task_replay_snapshots_task_id ON task_replay_snapshots (task_id);
CREATE INDEX ix_task_replay_snapshots_tenant_id ON task_replay_snapshots (tenant_id);


CREATE TABLE tool_calls (
	id VARCHAR(36) NOT NULL, 
	task_id VARCHAR(36) NOT NULL, 
	agent_run_id VARCHAR(36), 
	tool_name VARCHAR(64) NOT NULL, 
	arguments JSON NOT NULL, 
	result_summary TEXT, 
	result JSON,
	result_digest VARCHAR(64),
	status VARCHAR(32) NOT NULL, 
	latency_ms INTEGER, 
	policy_decision VARCHAR(32), 
	replay_policy VARCHAR(16),
	side_effect_level VARCHAR(16),
	idempotency_key VARCHAR(128),
	replayed BOOLEAN NOT NULL,
	approval_reference VARCHAR(128),
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_tool_call_task ON tool_calls (task_id);
CREATE INDEX ix_tool_calls_agent_run_id ON tool_calls (agent_run_id);
CREATE INDEX ix_tool_calls_idempotency_key ON tool_calls (idempotency_key);
CREATE INDEX ix_tool_calls_task_id ON tool_calls (task_id);


CREATE TABLE documents (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	kb_id VARCHAR(36) NOT NULL, 
	name VARCHAR(255) NOT NULL, 
	source_name VARCHAR(255) NOT NULL, 
	source_type VARCHAR(32) NOT NULL, 
	storage_key VARCHAR(512), 
	mime_type VARCHAR(128), 
	size_bytes INTEGER NOT NULL, 
	department VARCHAR(64), 
	security_level VARCHAR(32) NOT NULL, 
	document_version VARCHAR(32) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	error_message TEXT, 
	doc_updated_at DATE, 
	meta JSON NOT NULL, 
	uploaded_by VARCHAR(36), 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(kb_id) REFERENCES knowledge_bases (id)
);
CREATE INDEX ix_documents_kb_id ON documents (kb_id);
CREATE INDEX ix_documents_security_level ON documents (security_level);
CREATE INDEX ix_documents_status ON documents (status);
CREATE INDEX ix_documents_tenant_id ON documents (tenant_id);


CREATE TABLE users (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	username VARCHAR(64) NOT NULL, 
	email VARCHAR(128), 
	full_name VARCHAR(64), 
	hashed_password VARCHAR(255) NOT NULL, 
	department VARCHAR(64), 
	security_level INTEGER NOT NULL, 
	is_active BOOLEAN NOT NULL, 
	last_login_at TIMESTAMP WITHOUT TIME ZONE, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(tenant_id) REFERENCES tenants (id)
);
CREATE INDEX ix_users_tenant_id ON users (tenant_id);
CREATE UNIQUE INDEX ix_users_username ON users (username);


CREATE TABLE document_versions (
	id VARCHAR(36) NOT NULL, 
	tenant_id VARCHAR(36) NOT NULL, 
	document_id VARCHAR(36) NOT NULL, 
	version VARCHAR(32) NOT NULL, 
	storage_key VARCHAR(512), 
	checksum VARCHAR(128), 
	chunk_count INTEGER NOT NULL, 
	change_note TEXT, 
	created_by VARCHAR(36), 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id)
);
CREATE INDEX ix_document_versions_document_id ON document_versions (document_id);
CREATE INDEX ix_document_versions_tenant_id ON document_versions (tenant_id);


CREATE TABLE user_roles (
	id VARCHAR(36) NOT NULL, 
	user_id VARCHAR(36) NOT NULL, 
	role_code VARCHAR(64) NOT NULL, 
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_user_role UNIQUE (user_id, role_code), 
	FOREIGN KEY(user_id) REFERENCES users (id)
);
CREATE INDEX ix_user_roles_role_code ON user_roles (role_code);
CREATE INDEX ix_user_roles_user_id ON user_roles (user_id);

-- ---------------------------------------------------------------------------
-- 电商售后工单（需求书 §5 数据模型）
-- 新增于阶段改造：工单 / 消息 / 订单事实 / 处理建议 / 政策生效期
-- ---------------------------------------------------------------------------
CREATE TABLE tickets (
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
CREATE INDEX ix_ticket_tenant_created ON tickets (tenant_id, created_at);
CREATE INDEX ix_ticket_tenant_status ON tickets (tenant_id, status);
CREATE INDEX ix_tickets_assignee_id ON tickets (assignee_id);
CREATE INDEX ix_tickets_customer_id ON tickets (customer_id);
CREATE INDEX ix_tickets_external_id ON tickets (external_id);
CREATE INDEX ix_tickets_idempotency_key ON tickets (idempotency_key);
CREATE INDEX ix_tickets_issue_type ON tickets (issue_type);
CREATE INDEX ix_tickets_last_analysis_task_id ON tickets (last_analysis_task_id);
CREATE INDEX ix_tickets_order_id ON tickets (order_id);
CREATE INDEX ix_tickets_platform ON tickets (platform);

CREATE INDEX ix_tickets_quality_status ON tickets (quality_status);
CREATE INDEX ix_tickets_shop_id ON tickets (shop_id);
CREATE INDEX ix_tickets_status ON tickets (status);
CREATE INDEX ix_tickets_tenant_id ON tickets (tenant_id);
CREATE TABLE ticket_messages (
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
CREATE INDEX ix_message_ticket_created ON ticket_messages (ticket_id, created_at);
CREATE INDEX ix_ticket_messages_ticket_id ON ticket_messages (ticket_id);
CREATE INDEX ix_ticket_messages_idempotency_key ON ticket_messages (idempotency_key);
CREATE TABLE order_facts (
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
	PRIMARY KEY (id)
);
CREATE INDEX ix_fact_ticket_source ON order_facts (ticket_id, source);
CREATE INDEX ix_order_facts_request_id ON order_facts (request_id);
CREATE INDEX ix_order_facts_ticket_id ON order_facts (ticket_id);
CREATE TABLE recommendations (
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
	PRIMARY KEY (id),
	CONSTRAINT uq_reco_ticket_analysis_task UNIQUE (ticket_id, analysis_task_id)
);
CREATE INDEX ix_reco_analysis_task ON recommendations (analysis_task_id);
CREATE INDEX ix_reco_ticket_status ON recommendations (ticket_id, status);
CREATE INDEX ix_recommendations_idempotency_key ON recommendations (idempotency_key);
CREATE INDEX ix_recommendations_risk_tier ON recommendations (risk_tier);
CREATE INDEX ix_recommendations_status ON recommendations (status);
CREATE INDEX ix_recommendations_ticket_id ON recommendations (ticket_id);
CREATE TABLE policy_documents (
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
CREATE INDEX ix_policy_documents_document_id ON policy_documents (document_id);
CREATE INDEX ix_policy_documents_tenant_id ON policy_documents (tenant_id);
CREATE INDEX ix_policy_effective ON policy_documents (effective_from, effective_to);
CREATE INDEX ix_policy_tenant_scope ON policy_documents (tenant_id, status);
CREATE TABLE ticket_reply_templates (
	id VARCHAR(36) NOT NULL,
	tenant_id VARCHAR(36) NOT NULL,
	title VARCHAR(100) NOT NULL,
	content TEXT NOT NULL,
	issue_type VARCHAR(32),
	active BOOLEAN NOT NULL,
	created_by VARCHAR(36),
	updated_by VARCHAR(36),
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_reply_template_tenant_active ON ticket_reply_templates (tenant_id, active);
CREATE INDEX ix_ticket_reply_templates_issue_type ON ticket_reply_templates (issue_type);
CREATE INDEX ix_ticket_reply_templates_tenant_id ON ticket_reply_templates (tenant_id);
CREATE TABLE qa_conversations (
	id VARCHAR(36) NOT NULL,
	tenant_id VARCHAR(36) NOT NULL,
	user_id VARCHAR(36) NOT NULL,
	title VARCHAR(120) NOT NULL,
	last_message_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id)
);
CREATE INDEX ix_qa_conversations_tenant_id ON qa_conversations (tenant_id);
CREATE INDEX ix_qa_conversations_user_id ON qa_conversations (user_id);
CREATE INDEX ix_qa_conversations_last_message_at ON qa_conversations (last_message_at);
CREATE INDEX ix_qa_conversation_owner_updated ON qa_conversations (tenant_id, user_id, last_message_at);
CREATE TABLE qa_messages (
	id VARCHAR(36) NOT NULL,
	conversation_id VARCHAR(36) NOT NULL,
	tenant_id VARCHAR(36) NOT NULL,
	user_id VARCHAR(36) NOT NULL,
	sequence INTEGER NOT NULL,
	query TEXT NOT NULL,
	answer JSON NOT NULL,
	request_options JSON NOT NULL,
	source_audit_id VARCHAR(36),
	created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_qa_message_conversation_sequence UNIQUE (conversation_id, sequence)
);
CREATE INDEX ix_qa_messages_conversation_id ON qa_messages (conversation_id);
CREATE INDEX ix_qa_messages_tenant_id ON qa_messages (tenant_id);
CREATE INDEX ix_qa_messages_user_id ON qa_messages (user_id);
CREATE UNIQUE INDEX ix_qa_messages_source_audit_id ON qa_messages (source_audit_id);
CREATE INDEX ix_qa_message_conversation_created ON qa_messages (conversation_id, created_at);
CREATE TABLE operational_alerts (
	id VARCHAR(36) NOT NULL,
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
	PRIMARY KEY (id),
	CONSTRAINT uq_operational_alert_fingerprint UNIQUE (tenant_id, fingerprint)
);
CREATE INDEX ix_operational_alerts_tenant_id ON operational_alerts (tenant_id);
CREATE INDEX ix_operational_alerts_kind ON operational_alerts (kind);
CREATE INDEX ix_operational_alerts_source_id ON operational_alerts (source_id);
CREATE INDEX ix_operational_alerts_acknowledged_by ON operational_alerts (acknowledged_by);
CREATE INDEX ix_operational_alerts_resolved_by ON operational_alerts (resolved_by);
CREATE INDEX ix_operational_alert_tenant_status ON operational_alerts (tenant_id, status);
CREATE INDEX ix_operational_alert_severity ON operational_alerts (severity, last_occurred_at);
CREATE TABLE operational_alert_deliveries (
	id VARCHAR(36) NOT NULL,
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
	PRIMARY KEY (id),
	CONSTRAINT uq_operational_alert_delivery_occurrence UNIQUE (alert_id, occurrence_no, channel)
);
CREATE INDEX ix_operational_alert_deliveries_alert_id ON operational_alert_deliveries (alert_id);
CREATE INDEX ix_operational_alert_deliveries_tenant_id ON operational_alert_deliveries (tenant_id);
CREATE INDEX ix_operational_alert_deliveries_claimed_by ON operational_alert_deliveries (claimed_by);
CREATE INDEX ix_operational_alert_deliveries_lease_expires_at ON operational_alert_deliveries (lease_expires_at);
CREATE INDEX ix_alert_delivery_status_visible ON operational_alert_deliveries (status, visible_at);
CREATE INDEX ix_alert_delivery_tenant_status ON operational_alert_deliveries (tenant_id, status);
