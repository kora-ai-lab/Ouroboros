pub mod code_executor;

use std::fs;
use std::path::PathBuf;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SavedTool {
    pub name: String,
    pub description: String,
    pub language: String,
    pub path: PathBuf,
    pub created_at: String,
    pub last_used: Option<String>,
    pub use_count: u32,
}

pub struct ToolWorkspace {
    #[allow(dead_code)]
    pub root: PathBuf,
    pub tools_dir: PathBuf,
}

impl ToolWorkspace {
    pub fn new(app_data: PathBuf) -> Self {
        let tools_dir = app_data.join("tools");
        fs::create_dir_all(&tools_dir).ok();
        Self { root: app_data, tools_dir }
    }

    pub fn save_tool(&self, name: &str, code: &str, language: &str, description: &str) -> Result<SavedTool, String> {
        let ext = match language {
            "python" | "py" => "py",
            "node" | "js" | "javascript" => "js",
            "powershell" | "ps1" => "ps1",
            _ => "sh",
        };
        let filename = format!("{}.{}", name.replace(['/', '\\', ' '], "_"), ext);
        let path = self.tools_dir.join(&filename);
        fs::write(&path, code).map_err(|e| format!("Cannot save tool: {}", e))?;

        Ok(SavedTool {
            name: name.to_string(),
            description: description.to_string(),
            language: language.to_string(),
            path,
            created_at: now_iso(),
            last_used: None,
            use_count: 0,
        })
    }

    #[allow(dead_code)]
    pub fn list_tools(&self) -> Result<Vec<SavedTool>, String> {
        let mut tools = Vec::new();
        if !self.tools_dir.exists() {
            return Ok(tools);
        }
        for entry in fs::read_dir(&self.tools_dir).map_err(|e| format!("Cannot list tools: {}", e))? {
            let entry = entry.map_err(|e| format!("{}", e))?;
            let path = entry.path();
            if path.is_file() {
                let name = path.file_stem().unwrap_or_default().to_string_lossy().to_string();
                tools.push(SavedTool {
                    name,
                    description: String::new(),
                    language: String::from("shell"),
                    path,
                    created_at: String::new(),
                    last_used: None,
                    use_count: 0,
                });
            }
        }
        Ok(tools)
    }

    pub fn delete_tool(&self, name: &str) -> Result<(), String> {
        let path = self.tools_dir.join(&name);
        fs::remove_file(path).map_err(|e| format!("Cannot delete tool: {}", e))?;
        Ok(())
    }
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