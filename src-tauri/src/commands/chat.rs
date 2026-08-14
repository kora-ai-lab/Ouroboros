use crate::commands::auth as cloud;
use crate::events;
use crate::llm::validation::{
    retry_with_validation, CritiqueRoute, ModelResponse, ValidationContext, ValidationFailure,
};
use rusqlite::{params, Connection};
use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager};
use uuid::Uuid;

#[derive(Serialize, Clone)]
pub struct ChatPayload {
    pub conversation_id: String,
    pub token: String,
    pub index: u32,
}

#[derive(Serialize, Clone)]
pub struct ChatDonePayload {
    pub conversation_id: String,
    pub message_id: String,
    pub full_text: String,
}

#[derive(Serialize)]
pub struct ConversationResponse {
    pub id: String,
    pub title: String,
    pub model_id: String,
    pub created_at: String,
    pub updated_at: String,
}

#[derive(Serialize)]
pub struct MessageResponse {
    pub id: String,
    pub conversation_id: String,
    pub role: String,
    pub content: String,
    pub tool_calls_json: Option<String>,
    pub token_count: i32,
    pub created_at: String,
}

#[tauri::command]
pub async fn send_message(
    app: AppHandle,
    message: String,
    conversation_id: Option<String>,
    model_id: Option<String>,
) -> Result<ConversationResponse, String> {
    let conv_id = conversation_id.unwrap_or_else(|| Uuid::new_v4().to_string());
    let model = model_id.unwrap_or_else(|| "local/default".to_string());
    let msg = message.clone();
    let title = if msg.len() > 60 {
        msg[..60].to_string()
    } else {
        msg.clone()
    };
    let cid = conv_id.clone();

    let conversation_exists: bool = {
        let state = app.state::<crate::state::AppState>();
        let conn = state.db.conn();
        conn.query_row(
            "SELECT COUNT(*) FROM conversations WHERE id = ?1",
            params![cid],
            |row| row.get::<_, i32>(0),
        )
        .map(|c| c > 0)
        .unwrap_or(false)
    };

    {
        let state = app.state::<crate::state::AppState>();
        let conn = state.db.conn();
        if conversation_exists {
            conn.execute(
                "UPDATE conversations SET updated_at = datetime('now') WHERE id = ?1",
                params![cid],
            )
            .map_err(|e| e.to_string())?;
        } else {
            conn.execute(
                "INSERT INTO conversations (id, title, model_id) VALUES (?1, ?2, ?3)",
                params![cid, title, model],
            )
            .map_err(|e| e.to_string())?;
        }
        let user_msg_id = Uuid::new_v4().to_string();
        conn.execute("INSERT INTO messages (id, conversation_id, role, content, token_count) VALUES (?1, ?2, 'user', ?3, ?4)", params![user_msg_id, cid, &msg, msg.len() as i32])
            .map_err(|e| e.to_string())?;
    }

    let is_cloud = model.starts_with("cloud/");

    if is_cloud {
        let provider = {
            let parts: Vec<&str> = model.split('/').collect();
            if parts.len() >= 3 {
                parts[1].to_string()
            } else {
                "openai".to_string()
            }
        };
        let app_c = app.clone();
        let msg_c = msg.clone();
        let cid_c = cid.clone();

        tokio::spawn(async move {
            let r = cloud::stream_from_cloud(&app_c, &provider, &msg_c, &cid_c).await;
            if let Err(e) = r {
                let _ = app_c.emit(
                    events::CHAT_TOKEN,
                    ChatPayload {
                        conversation_id: cid_c.clone(),
                        token: format!("Error: {}", e),
                        index: 0,
                    },
                );
                let _ = app_c.emit(
                    events::CHAT_DONE,
                    ChatDonePayload {
                        conversation_id: cid_c,
                        message_id: String::new(),
                        full_text: format!("Error: {}", e),
                    },
                );
            }
        });

        return Ok(ConversationResponse {
            id: conv_id,
            title,
            model_id: model,
            created_at: now_iso(),
            updated_at: now_iso(),
        });
    }

    let app_clone = app.clone();
    tokio::spawn(async move {
        let state = app_clone.state::<crate::state::AppState>();
        let critique_route = configured_critique_route(&app_clone);
        let original_msg = msg.clone();
        let outcome = retry_with_validation(
            move |attempt| {
                let original_msg = original_msg.clone();
                async move {
                    let prompt = if let Some(correction) = attempt.corrective_system_message {
                        format!("System correction: {correction}\n\nUser request: {original_msg}")
                    } else {
                        original_msg
                    };
                    ModelResponse::text(generate_response(&prompt))
                }
            },
            ValidationContext::default(),
            critique_route,
        )
        .await;

        if !outcome.failures.is_empty() {
            let conn = state.db.conn();
            for failure in &outcome.failures {
                let _ = insert_validation_observation(&conn, &cid, failure);
            }
        }

        let response_text = outcome.response.content;
        let assistant_msg_id = Uuid::new_v4().to_string();
        let mut accumulated = String::new();

        let words: Vec<&str> = response_text.split_whitespace().collect();
        for (i, word) in words.iter().enumerate() {
            let token = if i == 0 {
                word.to_string()
            } else {
                format!(" {}", word)
            };
            let _ = app_clone.emit(
                events::CHAT_TOKEN,
                ChatPayload {
                    conversation_id: cid.clone(),
                    token: token.clone(),
                    index: i as u32,
                },
            );
            accumulated.push_str(&token);
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
        }

        {
            let conn = state.db.conn();
            let _ = conn.execute("INSERT INTO messages (id, conversation_id, role, content, token_count) VALUES (?1, ?2, 'assistant', ?3, ?4)",
                params![assistant_msg_id, cid, accumulated, accumulated.len() as i32]);
        }

        let _ = app_clone.emit(
            events::CHAT_DONE,
            ChatDonePayload {
                conversation_id: cid,
                message_id: assistant_msg_id,
                full_text: accumulated,
            },
        );
    });

    Ok(ConversationResponse {
        id: conv_id,
        title,
        model_id: model,
        created_at: now_iso(),
        updated_at: now_iso(),
    })
}

#[tauri::command]
pub async fn get_conversations(app: AppHandle) -> Result<Vec<ConversationResponse>, String> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();
    let mut stmt = conn.prepare("SELECT id, title, model_id, created_at, updated_at FROM conversations ORDER BY updated_at DESC LIMIT 50").map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map([], |row| {
            Ok(ConversationResponse {
                id: row.get(0)?,
                title: row.get(1)?,
                model_id: row.get(2)?,
                created_at: row.get(3)?,
                updated_at: row.get(4)?,
            })
        })
        .map_err(|e| e.to_string())?;
    let mut conversations = Vec::new();
    for row in rows {
        conversations.push(row.map_err(|e| e.to_string())?);
    }
    Ok(conversations)
}

#[tauri::command]
pub async fn get_messages(
    app: AppHandle,
    conversation_id: String,
) -> Result<Vec<MessageResponse>, String> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();
    let mut stmt = conn.prepare("SELECT id, conversation_id, role, content, tool_calls_json, COALESCE(token_count, 0), created_at FROM messages WHERE conversation_id = ?1 ORDER BY created_at ASC").map_err(|e| e.to_string())?;
    let rows = stmt
        .query_map(params![conversation_id], |row| {
            Ok(MessageResponse {
                id: row.get(0)?,
                conversation_id: row.get(1)?,
                role: row.get(2)?,
                content: row.get(3)?,
                tool_calls_json: row.get(4)?,
                token_count: row.get(5)?,
                created_at: row.get(6)?,
            })
        })
        .map_err(|e| e.to_string())?;
    let mut messages = Vec::new();
    for row in rows {
        messages.push(row.map_err(|e| e.to_string())?);
    }
    Ok(messages)
}

fn now_iso() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    let tm = secs / 86400;
    let y = 1970 + (tm / 365);
    let d = tm % 365;
    let mo = (d / 30) + 1;
    let day = (d % 30) + 1;
    let h = (secs % 86400) / 3600;
    let m = (secs % 3600) / 60;
    let s = secs % 60;
    format!("{y:04}-{mo:02}-{day:02}T{h:02}:{m:02}:{s:02}")
}

pub(crate) fn insert_validation_observation(
    conn: &Connection,
    conversation_id: &str,
    failure: &ValidationFailure,
) -> Result<(), rusqlite::Error> {
    let metadata_json = serde_json::json!({
        "source": "model_response_validator",
        "failure_kind": failure.kind.as_str(),
    })
    .to_string();

    conn.execute(
        "INSERT INTO task_observations (id, conversation_id, kind, detail, metadata_json) VALUES (?1, ?2, ?3, ?4, ?5)",
        params![
            Uuid::new_v4().to_string(),
            conversation_id,
            failure.kind.as_str(),
            failure.detail.as_str(),
            metadata_json,
        ],
    )?;
    Ok(())
}

fn configured_critique_route(app: &AppHandle) -> Option<CritiqueRoute> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();
    let provider = setting_string(&conn, "critique_provider")?;
    let model = setting_string(&conn, "critique_model")?;

    let has_key = conn
        .query_row(
            "SELECT COUNT(*) FROM api_keys WHERE provider = ?1",
            params![provider.as_str()],
            |row| row.get::<_, i32>(0),
        )
        .map(|count| count > 0)
        .unwrap_or(false);

    if has_key {
        Some(CritiqueRoute { provider, model })
    } else {
        None
    }
}

fn setting_string(conn: &Connection, key: &str) -> Option<String> {
    let value: String = conn
        .query_row(
            "SELECT value_json FROM settings WHERE key = ?1",
            params![key],
            |row| row.get(0),
        )
        .ok()?;

    serde_json::from_str::<String>(&value)
        .ok()
        .or_else(|| Some(value.trim_matches('"').to_string()))
        .filter(|s| !s.trim().is_empty())
}

#[tauri::command]
pub async fn delete_conversation(app: AppHandle, conversation_id: String) -> Result<(), String> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();
    conn.execute(
        "DELETE FROM conversations WHERE id = ?1",
        params![conversation_id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

#[tauri::command]
pub async fn update_conversation_title(
    app: AppHandle,
    conversation_id: String,
    title: String,
) -> Result<(), String> {
    let state = app.state::<crate::state::AppState>();
    let conn = state.db.conn();
    conn.execute(
        "UPDATE conversations SET title = ?1, updated_at = datetime('now') WHERE id = ?2",
        params![title, conversation_id],
    )
    .map_err(|e| e.to_string())?;
    Ok(())
}

fn generate_response(message: &str) -> String {
    let msg = message.to_lowercase();
    if msg.contains("ping") {
        return "I'm here and ready. I build my own tools to handle whatever you need. Tell me what to do.".to_string();
    }
    if msg.contains("hello") || msg.contains("hi") || msg.contains("hey") {
        return "Hello. I start with nothing but the ability to create tools for myself. Tell me what you need and I'll write a script, test it, and save it as a reusable tool.".to_string();
    }
    if msg.contains("list")
        && (msg.contains("file")
            || msg.contains("desktop")
            || msg.contains("directory")
            || msg.contains("folder"))
    {
        let cmd = if cfg!(target_os = "windows") {
            "dir /b %USERPROFILE%\\Desktop"
        } else {
            "ls -la $HOME/Desktop"
        };
        return format!("Let me build a file listing tool:\n```exec\n{}\n```", cmd);
    }
    if msg.contains("date") || msg.contains("time") || msg.contains("today") {
        let cmd = if cfg!(target_os = "windows") {
            "echo Date: %DATE% & echo Time: %TIME%"
        } else {
            "echo \"$(date)\""
        };
        return format!("```exec\n{}\n```", cmd);
    }
    if msg.contains("search") || msg.contains("find") {
        let cmd = if cfg!(target_os = "windows") {
            "dir /s /b *{}* 2>nul"
        } else {
            "find $HOME -name '*{}*' -type f 2>/dev/null | head -20"
        };
        return format!(
            "I'll create a file search tool. Edit the pattern inside the braces:\n```exec\n{}\n```",
            cmd
        );
    }
    if msg.contains("write") || msg.contains("create") || msg.contains("save") {
        return "I'll build a file creation tool:\n```exec\necho \"Created by Ouroboros\" > $HOME/Desktop/ouroboros-note.txt\necho \"Done.\"\n```".to_string();
    }
    if msg.contains("web")
        || msg.contains("http")
        || msg.contains("download")
        || msg.contains("api")
        || msg.contains("curl")
    {
        return "Here's a web request tool:\n```exec\ncurl -sL https://httpbin.org/get\n```\n\nRun it, and if it works, I'll save it as a reusable tool.".to_string();
    }
    if msg.contains("python") {
        return "I can write Python tools too:\n```python\nimport os, sys\nprint(f\"Python {sys.version}\")\nprint(f\"Home: {os.path.expanduser('~')}\")\n```".to_string();
    }
    "I don't have a tool for that yet, but I can build one.\n\n```exec\necho \"Tell me exactly what you need and I'll write the code.\"\n```\n\nEvery tool I build gets saved. Next time you ask for something similar, it'll already be ready.".to_string()
}
