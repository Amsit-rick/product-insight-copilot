-- Shared singleton leases for dedicated background schedulers.

CREATE TABLE IF NOT EXISTS scheduler_leases (
    name VARCHAR(64) PRIMARY KEY,
    owner_id VARCHAR(128),
    lease_expires_at TIMESTAMP WITHOUT TIME ZONE,
    heartbeat_at TIMESTAMP WITHOUT TIME ZONE,
    last_run_started_at TIMESTAMP WITHOUT TIME ZONE,
    last_run_finished_at TIMESTAMP WITHOUT TIME ZONE,
    last_status VARCHAR(32),
    last_error TEXT,
    last_result JSON NOT NULL DEFAULT '{}',
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL,
    updated_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_scheduler_leases_owner_id
    ON scheduler_leases (owner_id);
CREATE INDEX IF NOT EXISTS ix_scheduler_leases_lease_expires_at
    ON scheduler_leases (lease_expires_at);
