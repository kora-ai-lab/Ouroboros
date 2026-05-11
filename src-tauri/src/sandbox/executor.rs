use crate::sandbox::filesystem;
use crate::sandbox::permissions::RiskLevel;
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Serialize, Deserialize)]
pub struct ToolResult {
    pub success: bool,
    pub output: String,
    pub error: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct Sandbox {
    pub base_path: String,
}

impl Sandbox {
    pub fn new(base_path: String) -> Self {
        Self { base_path }
    }

    pub fn execute(
        &self,
        tool_name: &str,
        args: &serde_json::Value,
    ) -> Result<ToolResult, String> {
        match tool_name {
            "read_file" => {
                let path = args["path"].as_str().ok_or("Missing 'path' argument")?;
                let resolved = filesystem::resolve_safe_path(&self.base_path, path)?;
                let content = filesystem::read_file_contents(&resolved)?;
                let max_len = 20000;
                let truncated = if content.len() > max_len {
                    format!(
                        "{}\n\n[Truncated — {} more characters]",
                        &content[..max_len],
                        content.len() - max_len
                    )
                } else {
                    content
                };
                Ok(ToolResult {
                    success: true,
                    output: truncated,
                    error: None,
                })
            }
            "write_file" => {
                let path = args["path"].as_str().ok_or("Missing 'path' argument")?;
                let content = args["content"].as_str().ok_or("Missing 'content' argument")?;
                let resolved = filesystem::resolve_safe_path(&self.base_path, path)?;
                let backup = filesystem::backup_before_write(&resolved)?;
                filesystem::write_file_contents(&resolved, content)?;
                let msg = if let Some(b) = backup {
                    format!(
                        "File written: {}. Backup saved: {}",
                        resolved.display(),
                        b.file_name().unwrap_or_default().to_string_lossy()
                    )
                } else {
                    format!("File created: {}", resolved.display())
                };
                Ok(ToolResult {
                    success: true,
                    output: msg,
                    error: None,
                })
            }
            "list_directory" => {
                let path = args["path"]
                    .as_str()
                    .unwrap_or(&self.base_path);
                let resolved = filesystem::resolve_safe_path(&self.base_path, path)?;
                let entries = filesystem::list_directory(&resolved)?;
                if entries.is_empty() {
                    Ok(ToolResult {
                        success: true,
                        output: "Directory is empty.".to_string(),
                        error: None,
                    })
                } else {
                    Ok(ToolResult {
                        success: true,
                        output: entries.join("\n"),
                        error: None,
                    })
                }
            }
            "run_command" => {
                let cmd = args["command"].as_str().ok_or("Missing 'command' argument")?;
                let result = filesystem::run_command_sandboxed(cmd)?;
                Ok(ToolResult {
                    success: true,
                    output: result,
                    error: None,
                })
            }
            "search_files" => {
                let path = args["path"].as_str().unwrap_or(&self.base_path);
                let pattern = args["pattern"].as_str().ok_or("Missing 'pattern' argument")?;
                let resolved = filesystem::resolve_safe_path(&self.base_path, path)?;
                let results = filesystem::search_files(&resolved, pattern)?;
                if results.is_empty() {
                    Ok(ToolResult {
                        success: true,
                        output: format!("No files matching '{}' found.", pattern),
                        error: None,
                    })
                } else {
                    Ok(ToolResult {
                        success: true,
                        output: results.join("\n"),
                        error: None,
                    })
                }
            }
            _ => Err(format!("Unknown tool: {}", tool_name)),
        }
    }

    pub fn get_risk(tool_name: &str) -> RiskLevel {
        match tool_name {
            "read_file" => RiskLevel::Safe,
            "write_file" => RiskLevel::Caution,
            "list_directory" => RiskLevel::Safe,
            "run_command" => RiskLevel::Destructive,
            "search_files" => RiskLevel::Safe,
            _ => RiskLevel::Caution,
        }
    }
}

pub fn default_base_path() -> String {
    dirs::home_dir()
        .unwrap_or_else(|| Path::new(".").to_path_buf())
        .to_string_lossy()
        .to_string()
}