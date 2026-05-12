use rusqlite::Connection;

pub fn apply(conn: &Connection) -> Result<(), Box<dyn std::error::Error>> {
    conn.execute_batch(
        "
        CREATE TABLE IF NOT EXISTS task_observations (
            id              TEXT PRIMARY KEY,
            conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
            kind            TEXT NOT NULL,
            detail          TEXT NOT NULL,
            metadata_json   TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_task_observations_conversation
            ON task_observations(conversation_id, created_at ASC);

        INSERT INTO schema_version (version, applied_at) VALUES (5, datetime('now'));
        ",
    )?;
    log::info!("Applied database migration v5 - added task observations");
    Ok(())
}
