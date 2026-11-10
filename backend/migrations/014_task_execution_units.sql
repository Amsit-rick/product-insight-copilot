-- Persistent parent/child task execution units and ownership policies.

CREATE TABLE IF NOT EXISTS task_execution_units (
    id VARCHAR(36) PRIMARY KEY,
    task_id VARCHAR(36) NOT NULL,
    parent_unit_id VARCHAR(36),
    name VARCHAR(255) NOT NULL,
    unit_type VARCHAR(24) NOT NULL,
    owner_type VARCHAR(24) NOT NULL,
    owner_ref_id VARCHAR(64) NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    execution_mode VARCHAR(16) NOT NULL DEFAULT 'attached',
    cancellation_policy VARCHAR(16) NOT NULL DEFAULT 'cascade',
    failure_policy VARCHAR(16) NOT NULL DEFAULT 'continue',
    metadata JSON NOT NULL DEFAULT '{}',
    started_at TIMESTAMP WITHOUT TIME ZONE,
    finished_at TIMESTAMP WITHOUT TIME ZONE,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    CONSTRAINT uq_task_execution_owner UNIQUE (task_id, owner_type, owner_ref_id)
);

CREATE INDEX IF NOT EXISTS ix_task_execution_parent
    ON task_execution_units (task_id, parent_unit_id);
CREATE INDEX IF NOT EXISTS ix_task_execution_status
    ON task_execution_units (task_id, status);
CREATE INDEX IF NOT EXISTS ix_task_execution_units_owner_ref_id
    ON task_execution_units (owner_ref_id);
CREATE INDEX IF NOT EXISTS ix_task_execution_units_owner_type
    ON task_execution_units (owner_type);
CREATE INDEX IF NOT EXISTS ix_task_execution_units_parent_unit_id
    ON task_execution_units (parent_unit_id);
CREATE INDEX IF NOT EXISTS ix_task_execution_units_task_id
    ON task_execution_units (task_id);
CREATE INDEX IF NOT EXISTS ix_task_execution_units_unit_type
    ON task_execution_units (unit_type);

INSERT INTO task_execution_units (
    id,
    task_id,
    parent_unit_id,
    name,
    unit_type,
    owner_type,
    owner_ref_id,
    status,
    execution_mode,
    cancellation_policy,
    failure_policy,
    metadata,
    started_at,
    finished_at,
    created_at,
    updated_at
)
SELECT
    task.id,
    task.id,
    NULL,
    COALESCE(task.title, '主任务'),
    'workflow',
    'workflow',
    task.id,
    CASE task.status
        WHEN 'running' THEN 'running'
        WHEN 'waiting_human' THEN 'waiting'
        WHEN 'completed' THEN 'succeeded'
        WHEN 'failed' THEN 'failed'
        WHEN 'cancelled' THEN 'cancelled'
        ELSE 'pending'
    END,
    'attached',
    'cascade',
    'continue',
    json_build_object('query', LEFT(task.query, 500)),
    task.started_at,
    task.finished_at,
    task.created_at,
    task.updated_at
FROM analysis_tasks AS task
ON CONFLICT DO NOTHING;
