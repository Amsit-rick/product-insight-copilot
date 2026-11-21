-- Preserve the upstream fact mapping version for audit and task replay.
-- Existing rows remain NULL because their source version was not captured.

ALTER TABLE order_facts
    ADD COLUMN IF NOT EXISTS source_version VARCHAR(64);
