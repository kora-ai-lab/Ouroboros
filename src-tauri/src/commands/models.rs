use crate::llm::hardware;
use serde::Serialize;
use tauri::AppHandle;

#[derive(Serialize)]
pub struct ModelInfo {
    pub id: String,
    pub name: String,
    pub size_mb: u64,
    pub downloaded: bool,
    pub provider: String,
}

#[tauri::command]
pub async fn get_hardware_info() -> Result<hardware::HardwareInfo, String> {
    Ok(hardware::detect())
}

#[tauri::command]
pub async fn list_models(_app: AppHandle) -> Result<Vec<ModelInfo>, String> {
    let info = hardware::detect();
    let recommended = info.recommended_model;

    Ok(vec![
        ModelInfo {
            id: "local/default".to_string(),
            name: format!("Local ({})", recommended),
            size_mb: 2000,
            downloaded: true,
            provider: "local".to_string(),
        },
        ModelInfo {
            id: "cloud/openai/gpt-4o-mini".to_string(),
            name: "GPT-4o Mini".to_string(),
            size_mb: 0,
            downloaded: false,
            provider: "openai".to_string(),
        },
        ModelInfo {
            id: "cloud/anthropic/claude-sonnet-4".to_string(),
            name: "Claude Sonnet 4".to_string(),
            size_mb: 0,
            downloaded: false,
            provider: "anthropic".to_string(),
        },
        ModelInfo {
            id: "cloud/google/gemini-2.0-flash".to_string(),
            name: "Gemini 2.0 Flash".to_string(),
            size_mb: 0,
            downloaded: false,
            provider: "google".to_string(),
        },
    ])
}