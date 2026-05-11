use rusqlite::Connection;

pub fn apply(conn: &Connection) -> Result<(), Box<dyn std::error::Error>> {
    conn.execute_batch(
        "
        ALTER TABLE tool_registry ADD COLUMN language TEXT NOT NULL DEFAULT 'shell';
        INSERT INTO schema_version (version, applied_at) VALUES (2, datetime('now'));
        "
    )?;
    log::info!("Applied database migration v2");
    Ok(())
}