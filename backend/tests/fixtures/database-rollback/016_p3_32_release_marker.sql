-- Rehearsal-only migration used to prove snapshot restore across schema changes.
CREATE TABLE release_migration_probe (
    id INTEGER PRIMARY KEY,
    note TEXT NOT NULL,
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT now() NOT NULL
);

