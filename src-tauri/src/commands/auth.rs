use crate::crypto;
use crate::llm::validation::{retry_with_validation, ModelResponse, ValidationContext};
use rusqlite::params;
use serde::{Deserialize, Serialize};
use tauri::{AppHandle, Emitter, Manager};

#[derive(Serialize, Deserialize, Clone)]
pub struct ProviderConfig {
    pub id: String,
    pub provider: String,
    pub model_id: String,
    pub label: Option<String>,
    pub connected: bool,
}

#[tauri::command]
pub async fn add_api_key(
    app: AppHandle,
    provider: String,
    key: String,
    label: Option<String>,
) -> Result<ProviderConfig, String> {
    let encrypted = crypto::encrypt(&key)?;
    let id = uuid::Uuid::new_v4().to_string();

    {
        let state = app.state::<crate::state::AppState>();
        let conn = state.db.conn();
        conn.execute(
            "INSERT OR REPLACE INTO api_keys (id, provider, label, key_encrypted) VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![id, provider, label, encrypted],
        )
        .map_err(|e| e.to_string())?;
    }

    Ok(ProviderConfig {
        id,
        provider: provider.clone(),
        model_id: default_model_for_provider(&provider),
        label,
        connected: true,
    })
}

#[tauri::command]
pub async fn remove_api_key(app: AppHandle, id: String) -> Result<(), String> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();
    conn.execute("DELETE FROM api_keys WHERE id = ?1", params![id])
        .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub async fn list_providers(app: AppHandle) -> Result<Vec<ProviderConfig>, String> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();

    let mut stmt = conn
        .prepare("SELECT id, provider, label FROM api_keys ORDER BY provider")
        .map_err(|e| e.to_string())?;

    let providers = stmt
        .query_map([], |row| {
            Ok(ProviderConfig {
                id: row.get(0)?,
                provider: row.get(1)?,
                model_id: default_model_for_provider(&row.get::<_, String>(1)?),
                label: row.get(2)?,
                connected: true,
            })
        })
        .map_err(|e| e.to_string())?
        .filter_map(|r| r.ok())
        .collect();

    Ok(providers)
}

fn default_model_for_provider(provider: &str) -> String {
    match provider {
        "openai" => "gpt-4o-mini".to_string(),
        "anthropic" => "claude-sonnet-4-20250514".to_string(),
        "google" => "gemini-2.0-flash".to_string(),
        _ => "default".to_string(),
    }
}

fn get_api_key(app: &AppHandle, provider: &str) -> Result<String, String> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();
    let encrypted = conn
        .query_row(
            "SELECT key_encrypted FROM api_keys WHERE provider = ?1 LIMIT 1",
            params![provider],
            |row| row.get::<_, String>(0),
        )
        .map_err(|e| format!("No API key found for '{}': {}", provider, e))?;
    crypto::decrypt(&encrypted)
}

pub async fn stream_from_cloud(
    app: &AppHandle,
    provider: &str,
    message: &str,
    conversation_id: &str,
) -> Result<(), String> {
    let api_key = get_api_key(app, provider)?;
    let api_key_for_retry = api_key.clone();
    let provider_for_retry = provider.to_string();
    let original_message = message.to_string();

    let outcome = retry_with_validation(
        move |attempt| {
            let api_key = api_key_for_retry.clone();
            let provider = provider_for_retry.clone();
            let original_message = original_message.clone();
            async move {
                let prompt = if let Some(correction) = attempt.corrective_system_message {
                    format!("System correction: {correction}\n\nUser request: {original_message}")
                } else {
                    original_message
                };

                match complete_from_cloud_provider(&provider, &api_key, &prompt).await {
                    Ok(content) => ModelResponse::text(content),
                    Err(error) => ModelResponse::text(format!("Error: {error}")),
                }
            }
        },
        ValidationContext::default(),
        None,
    )
    .await;

    if !outcome.failures.is_empty() {
        let state = app.state::<crate::state::AppState>();
        let conn = state.db.conn();
        for failure in &outcome.failures {
            let _ = crate::commands::chat::insert_validation_observation(
                &conn,
                conversation_id,
                failure,
            );
        }
    }

    emit_tokens(app, conversation_id, &outcome.response.content);
    Ok(())
}

async fn complete_from_cloud_provider(
    provider: &str,
    api_key: &str,
    message: &str,
) -> Result<String, String> {
    match provider {
        "openai" => complete_openai(api_key, message).await,
        "anthropic" => complete_anthropic(api_key, message).await,
        "google" => complete_google(api_key, message).await,
        _ => Err(format!("Unknown provider: {}", provider)),
    }
}

async fn complete_openai(api_key: &str, message: &str) -> Result<String, String> {
    let client = reqwest::Client::new();
    let body = serde_json::json!({
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": message}],
        "stream": false
    });

    let resp = client
        .post("https://api.openai.com/v1/chat/completions")
        .header("Authorization", format!("Bearer {}", api_key))
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("OpenAI request failed: {}", e))?;

    let json: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("Parse error: {}", e))?;
    Ok(json["choices"][0]["message"]["content"]
        .as_str()
        .unwrap_or("No response")
        .to_string())
}

async fn complete_anthropic(api_key: &str, message: &str) -> Result<String, String> {
    let client = reqwest::Client::new();
    let body = serde_json::json!({
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": message}]
    });

    let resp = client
        .post("https://api.anthropic.com/v1/messages")
        .header("x-api-key", api_key)
        .header("anthropic-version", "2023-06-01")
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Anthropic request failed: {}", e))?;

    let json: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("Parse error: {}", e))?;
    Ok(json["content"][0]["text"]
        .as_str()
        .unwrap_or("No response")
        .to_string())
}

async fn complete_google(api_key: &str, message: &str) -> Result<String, String> {
    let url = format!(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={}",
        api_key
    );
    let client = reqwest::Client::new();
    let body = serde_json::json!({
        "contents": [{"parts": [{"text": message}]}]
    });

    let resp = client
        .post(&url)
        .json(&body)
        .send()
        .await
        .map_err(|e| format!("Google request failed: {}", e))?;

    let json: serde_json::Value = resp
        .json()
        .await
        .map_err(|e| format!("Parse error: {}", e))?;
    Ok(json["candidates"][0]["content"]["parts"][0]["text"]
        .as_str()
        .unwrap_or("No response")
        .to_string())
}

fn emit_tokens(app: &AppHandle, conversation_id: &str, text: &str) {
    let app_clone = app.clone();
    let text_owned = text.to_string();
    let conversation_id = conversation_id.to_string();

    std::thread::spawn(move || {
        let words: Vec<String> = text_owned.split(' ').map(|w| w.to_string()).collect();
        for (i, word) in words.iter().enumerate() {
            let token = if i == 0 {
                word.clone()
            } else {
                format!(" {}", word)
            };
            let _ = app_clone.emit(
                "chat:token",
                crate::commands::chat::ChatPayload {
                    conversation_id: conversation_id.clone(),
                    token,
                    index: i as u32,
                },
            );
            std::thread::sleep(std::time::Duration::from_millis(15));
        }
        let _ = app_clone.emit(
            "chat:done",
            crate::commands::chat::ChatDonePayload {
                conversation_id: conversation_id.clone(),
                message_id: uuid::Uuid::new_v4().to_string(),
                full_text: text_owned,
            },
        );
    });
}
