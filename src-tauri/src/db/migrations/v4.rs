use rusqlite::Connection;

pub fn apply(conn: &Connection) -> Result<(), Box<dyn std::error::Error>> {
    conn.execute_batch(
        "
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value_json TEXT NOT NULL,
            updated_at DATETIME DEFAULT (datetime('now'))
        );
        INSERT INTO settings (key, value_json) VALUES ('theme', 'dark') ON CONFLICT DO NOTHING;
        INSERT INTO settings (key, value_json) VALUES ('auto_start', 'false') ON CONFLICT DO NOTHING;
        INSERT INTO settings (key, value_json) VALUES ('bubble_position_x', '100') ON CONFLICT DO NOTHING;
        INSERT INTO settings (key, value_json) VALUES ('bubble_position_y', '100') ON CONFLICT DO NOTHING;
        
        CREATE TABLE IF NOT EXISTS tool_permissions (
            tool_name TEXT PRIMARY KEY,
            default_permission TEXT NOT NULL CHECK (default_permission IN ('ask', 'allow', 'deny')),
            updated_at DATETIME DEFAULT (datetime('now'))
        );
        INSERT INTO schema_version (version, applied_at) VALUES (4, datetime('now'));
        "
    )?;
    log::info!("Applied database migration v4 - added settings and tool_permissions tables");
    Ok(())
}