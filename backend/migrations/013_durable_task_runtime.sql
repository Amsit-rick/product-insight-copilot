-- Durable Agent runtime metadata and replay-safe tool contracts.

ALTER TABLE analysis_tasks ADD COLUMN IF NOT EXISTS current_phase VARCHAR(64);
ALTER TABLE analysis_tasks ADD COLUMN IF NOT EXISTS checkpoint_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_tasks ADD COLUMN IF NOT EXISTS phase_attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_tasks ADD COLUMN IF NOT EXISTS resume_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE analysis_tasks ADD COLUMN IF NOT EXISTS resumed_from_checkpoint BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE analysis_tasks ADD COLUMN IF NOT EXISTS wake_reason VARCHAR(64);
ALTER TABLE analysis_tasks ADD COLUMN IF NOT EXISTS state_revision INTEGER NOT NULL DEFAULT 0;

ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS replay_policy VARCHAR(16);
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS side_effect_level VARCHAR(16);
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128);
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS replayed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS approval_reference VARCHAR(128);

CREATE INDEX IF NOT EXISTS ix_tool_calls_idempotency_key ON tool_calls (idempotency_key);
