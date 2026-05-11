use std::path::{Path, PathBuf};
use std::fs;

const SAFE_DIRS: &[&str] = &["Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"];

pub fn resolve_safe_path(base: &str, user_requested: &str) -> Result<PathBuf, String> {
    let requested = Path::new(user_requested);

    if requested.is_absolute() {
        if !is_safe_absolute_path(requested) {
            return Err(format!(
                "Path '{}' is outside allowed directories. Access is restricted to: {:?}",
                user_requested, SAFE_DIRS
            ));
        }
        let canonical = fs::canonicalize(requested).map_err(|e| format!("Cannot access path: {}", e))?;
        if !is_safe_absolute_path(&canonical) {
            return Err("Path traversal detected — access denied.".to_string());
        }
        return Ok(canonical);
    }

    let resolved = Path::new(base).join(requested);
    let canonical = fs::canonicalize(&resolved).map_err(|e| format!("Cannot access path: {}", e))?;
    if !is_safe_absolute_path(&canonical) {
        return Err("Path traversal detected — access denied.".to_string());
    }
    Ok(canonical)
}

fn is_safe_absolute_path(path: &Path) -> bool {
    if cfg!(target_os = "windows") {
        if let Some(home) = dirs_next() {
            let docs = home.join("Documents");
            let desktop = home.join("Desktop");
            let downloads = home.join("Downloads");
            if path.starts_with(&home) {
                return path.starts_with(&docs)
                    || path.starts_with(&desktop)
                    || path.starts_with(&downloads)
                    || path == home;
            }
        }
        return false;
    }

    if let Some(home) = dirs::home_dir() {
        return path.starts_with(&home) || path.starts_with("/tmp");
    }
    false
}

fn dirs_next() -> Option<PathBuf> {
    dirs::home_dir()
}

pub fn backup_before_write(file_path: &Path) -> Result<Option<PathBuf>, String> {
    if !file_path.exists() {
        return Ok(None);
    }
    let backup = file_path.with_extension(format!(
        "bak.{}",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs()
    ));
    fs::copy(file_path, &backup).map_err(|e| format!("Failed to create backup: {}", e))?;
    Ok(Some(backup))
}

pub fn read_file_contents(path: &Path) -> Result<String, String> {
    fs::read_to_string(path).map_err(|e| format!("Cannot read file: {}", e))
}

pub fn write_file_contents(path: &Path, content: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("Cannot create directory: {}", e))?;
    }
    fs::write(path, content).map_err(|e| format!("Cannot write file: {}", e))?;
    Ok(())
}

use std::process::Command;

pub fn list_directory(path: &Path) -> Result<Vec<String>, String> {
    let mut entries = Vec::new();
    let dir = fs::read_dir(path).map_err(|e| format!("Cannot read directory: {}", e))?;
    for entry in dir {
        let entry = entry.map_err(|e| format!("Error reading entry: {}", e))?;
        let name = entry.file_name().to_string_lossy().to_string();
        let is_dir = entry.file_type().map(|t| t.is_dir()).unwrap_or(false);
        entries.push(if is_dir { format!("{}/", name) } else { name });
    }
    entries.sort();
    Ok(entries)
}

pub fn search_files(dir: &Path, pattern: &str) -> Result<Vec<String>, String> {
    let mut results = Vec::new();
    search_recursive(dir, pattern, &mut results, 0)
        .map_err(|e| format!("Search failed: {}", e))?;
    results.sort();
    Ok(results)
}

fn search_recursive(dir: &Path, pattern: &str, results: &mut Vec<String>, depth: u32) -> Result<(), String> {
    if depth > 5 {
        return Ok(());
    }
    let entries = fs::read_dir(dir).map_err(|e| format!("Cannot read directory: {}", e))?;
    for entry in entries {
        let entry = entry.map_err(|e| format!("Error reading entry: {}", e))?;
        let path = entry.path();
        let name = entry.file_name().to_string_lossy().to_lowercase();
        if name.contains(&pattern.to_lowercase()) {
            results.push(path.to_string_lossy().to_string());
        }
        if path.is_dir() {
            search_recursive(&path, pattern, results, depth + 1)?;
        }
    }
    Ok(())
}

pub fn run_command_sandboxed(cmd: &str) -> Result<String, String> {
    let forbidden: &[&str] = &["rm -rf", "del /", "format", "shutdown", "reboot", "sudo", "> ", ">> "];
    let cmd_lower = cmd.to_lowercase();
    for fb in forbidden {
        if cmd_lower.contains(fb) {
            return Err(format!("Command blocked: contains forbidden operation '{}'", fb));
        }
    }

    let output = if cfg!(target_os = "windows") {
        Command::new("cmd")
            .args(["/C", cmd])
            .output()
            .map_err(|e| format!("Cannot execute command: {}", e))?
    } else {
        Command::new("sh")
            .args(["-c", cmd])
            .output()
            .map_err(|e| format!("Cannot execute command: {}", e))?
    };

    let stdout = String::from_utf8_lossy(&output.stdout).to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).to_string();

    if stdout.is_empty() && !stderr.is_empty() {
        Err(format!("Command error: {}", stderr))
    } else if !stdout.is_empty() && !stderr.is_empty() {
        Ok(format!("{}\nErrors:\n{}", stdout, stderr))
    } else if stdout.is_empty() {
        Ok("Command completed with no output.".to_string())
    } else {
        Ok(stdout)
    }
}