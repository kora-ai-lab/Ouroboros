use serde_json;
use tauri::{AppHandle, Manager};

#[tauri::command]
pub async fn get_setting(app: AppHandle, key: String) -> Result<Option<String>, String> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();
    let result = conn.query_row(
        "SELECT value_json FROM settings WHERE key = ?1",
        [&key],
        |row| row.get::<_, String>(0),
    );
    match result {
        Ok(val) => Ok(Some(val)),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
        Err(e) => Err(e.to_string()),
    }
}

#[tauri::command]
pub async fn set_setting(app: AppHandle, key: String, value: serde_json::Value) -> Result<(), String> {
    let val_str = serde_json::to_string(&value).map_err(|e| e.to_string())?;
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value_json, updated_at) VALUES (?1, ?2, datetime('now'))",
        [&key, &val_str],
    ).map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub async fn get_all_settings(app: AppHandle) -> Result<Vec<(String, String)>, String> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();
    let mut stmt = conn
        .prepare("SELECT key, value_json FROM settings ORDER BY key")
        .map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([], |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)))
        .map_err(|e| e.to_string())?;
    let mut items = Vec::new();
    for row in rows {
        items.push(row.map_err(|e| e.to_string())?);
    }
    Ok(items)
}