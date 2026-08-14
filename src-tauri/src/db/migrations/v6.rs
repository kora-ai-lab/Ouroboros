use rusqlite::Connection;

pub fn apply(conn: &Connection) -> Result<(), Box<dyn std::error::Error>> {
    conn.execute_batch(
        "
        UPDATE messages SET token_count = 0 WHERE token_count IS NULL;
        INSERT INTO schema_version (version, applied_at) VALUES (6, datetime('now'));
        ",
    )?;
    log::info!("Applied database migration v6 - normalized nullable message token counts");
    Ok(())
}
