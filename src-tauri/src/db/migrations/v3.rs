use rusqlite::Connection;

pub fn apply(conn: &Connection) -> Result<(), Box<dyn std::error::Error>> {
    conn.execute_batch(
        "
        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            label TEXT,
            key_encrypted TEXT NOT NULL,
            created_at DATETIME DEFAULT (datetime('now'))
        );
        INSERT INTO schema_version (version, applied_at) VALUES (3, datetime('now'));
        "
    )?;
    log::info!("Applied database migration v3 - added api_keys table");
    Ok(())
}