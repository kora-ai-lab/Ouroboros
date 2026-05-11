use rusqlite::Connection;

pub fn apply(conn: &Connection) -> Result<(), Box<dyn std::error::Error>> {
    conn.execute_batch(
        "
        CREATE TABLE IF NOT EXISTS schema_version (
            version     INTEGER PRIMARY KEY,
            applied_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL DEFAULT 'New conversation',
            model_id    TEXT NOT NULL DEFAULT 'local/default',
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_conversations_updated
            ON conversations(updated_at DESC);

        CREATE TABLE IF NOT EXISTS messages (
            id                TEXT PRIMARY KEY,
            conversation_id   TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            role              TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'tool')),
            content           TEXT NOT NULL,
            tool_calls_json   TEXT,
            token_count       INTEGER DEFAULT 0,
            created_at        TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_messages_conversation
            ON messages(conversation_id, created_at ASC);

        CREATE TABLE IF NOT EXISTS api_keys (
            id              TEXT PRIMARY KEY,
            provider        TEXT NOT NULL,
            label           TEXT,
            key_encrypted   BLOB NOT NULL,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(provider, label)
        );

        CREATE TABLE IF NOT EXISTS settings (
            key         TEXT PRIMARY KEY,
            value_json  TEXT NOT NULL,
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS tool_registry (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL,
            schema_json TEXT NOT NULL,
            enabled     INTEGER NOT NULL DEFAULT 1,
            is_builtin  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_tools_enabled
            ON tool_registry(enabled) WHERE enabled = 1;

        INSERT INTO schema_version (version, applied_at) VALUES (1, datetime('now'));
        "
    )?;

    log::info!("Applied database migration v1");
    Ok(())
}