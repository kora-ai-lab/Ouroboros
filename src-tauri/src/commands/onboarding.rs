use tauri::{AppHandle, Manager};

#[tauri::command]
pub async fn complete_onboarding(app: AppHandle) -> Result<(), String> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value_json, updated_at) VALUES (?1, ?2, datetime('now'))",
        ["onboarding_completed", r#""true""#],
    )
    .map_err(|e| e.to_string())?;
    drop(conn);

    let main = app.get_webview_window("main").ok_or("main window not found")?;
    main.show().map_err(|e| e.to_string())?;

    if let Some(onboarding) = app.get_webview_window("onboarding") {
        onboarding.close().map_err(|e| e.to_string())?;
    }

    Ok(())
}