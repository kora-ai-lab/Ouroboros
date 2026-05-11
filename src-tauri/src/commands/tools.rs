use crate::sandbox::code_executor;
use crate::sandbox::ToolWorkspace;
use serde::Serialize;
use tauri::{AppHandle, Manager};

#[derive(Serialize)]
pub struct ToolEntry {
    pub name: String,
    pub description: String,
    pub language: String,
}

#[tauri::command]
pub async fn execute_code(
    _app: AppHandle,
    code: String,
    language: Option<String>,
    time_limit: Option<u64>,
) -> Result<code_executor::CodeResult, String> {
    let lang = language.unwrap_or_else(|| "shell".to_string());
    let working_dir = dirs::home_dir()
        .map(|p| p.to_string_lossy().to_string())
        .unwrap_or_else(|| ".".to_string());
    let limit = time_limit.unwrap_or(120);

    Ok(code_executor::execute(&code, &lang, &working_dir, limit))
}

#[tauri::command]
pub async fn list_tools(
    app: AppHandle,
) -> Result<Vec<ToolEntry>, String> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();

    let mut stmt = conn
        .prepare("SELECT name, description, language FROM tool_registry WHERE enabled = 1 ORDER BY name")
        .map_err(|e| e.to_string())?;

    let tools = stmt
        .query_map([], |row| {
            Ok(ToolEntry {
                name: row.get(0)?,
                description: row.get(1)?,
                language: row.get(2)?,
            })
        })
        .map_err(|e| e.to_string())?
        .filter_map(|r| r.ok())
        .collect();

    Ok(tools)
}

#[tauri::command]
pub async fn save_tool(
    app: AppHandle,
    name: String,
    code: String,
    language: String,
    description: String,
) -> Result<ToolEntry, String> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();

    let id = uuid::Uuid::new_v4().to_string();
    let schema = serde_json::json!({"type": "object"});

    conn.execute(
        "INSERT OR REPLACE INTO tool_registry (id, name, description, schema_json, language, enabled, is_builtin) VALUES (?1, ?2, ?3, ?4, ?5, 1, 0)",
        rusqlite::params![id, name, description, serde_json::to_string(&schema).unwrap(), language],
    )
    .map_err(|e| e.to_string())?;

    let app_data = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let workspace = ToolWorkspace::new(app_data);
    workspace.save_tool(&name, &code, &language, &description)?;

    Ok(ToolEntry { name, description, language })
}

#[tauri::command]
pub async fn delete_tool(
    app: AppHandle,
    tool_name: String,
) -> Result<(), String> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();
    conn.execute(
        "DELETE FROM tool_registry WHERE name = ?1",
        rusqlite::params![tool_name],
    )
    .map_err(|e| e.to_string())?;

    let app_data = app.path().app_data_dir().map_err(|e| e.to_string())?;
    let workspace = ToolWorkspace::new(app_data);
    workspace.delete_tool(&tool_name).ok();

    Ok(())
}