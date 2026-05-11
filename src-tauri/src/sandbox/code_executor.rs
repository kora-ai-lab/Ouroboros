use serde::{Deserialize, Serialize};
use std::process::{Command, Stdio};
use std::time::Duration;
use std::sync::mpsc;
use std::thread;

#[derive(Debug, Serialize, Deserialize)]
pub struct CodeResult {
    pub stdout: String,
    pub stderr: String,
    pub exit_code: i32,
    pub timed_out: bool,
}

pub fn execute(code: &str, language: &str, working_dir: &str, time_limit_secs: u64) -> CodeResult {
    let (command, args): (&str, Vec<String>) = match language {
        "python" | "py" => ("python", vec!["-c".into(), code.to_string()]),
        "node" | "js" | "javascript" => ("node", vec!["-e".into(), code.to_string()]),
        "powershell" | "ps1" => ("powershell", vec!["-NoProfile".into(), "-Command".into(), code.to_string()]),
        _ => {
            if cfg!(target_os = "windows") {
                ("cmd", vec!["/C".into(), code.to_string()])
            } else {
                ("sh", vec!["-c".into(), code.to_string()])
            }
        }
    };

    let (tx, rx) = mpsc::channel();
    let wd = working_dir.to_string();

    thread::spawn(move || {
        let mut child = match Command::new(command)
            .args(&args)
            .current_dir(&wd)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
        {
            Ok(c) => c,
            Err(e) => {
                let _ = tx.send(CodeResult { stdout: String::new(), stderr: format!("Cannot start: {}", e), exit_code: -1, timed_out: false });
                return;
            }
        };

        let start = std::time::Instant::now();
        loop {
            match child.try_wait() {
                Ok(Some(status)) => {
                    let mut stdout = String::new();
                    let mut stderr = String::new();
                    if let Some(mut out) = child.stdout.take() {
                        use std::io::Read;
                        let mut buf = Vec::new();
                        out.read_to_end(&mut buf).ok();
                        stdout = String::from_utf8_lossy(&buf).to_string();
                    }
                    if let Some(mut err) = child.stderr.take() {
                        use std::io::Read;
                        let mut buf = Vec::new();
                        err.read_to_end(&mut buf).ok();
                        stderr = String::from_utf8_lossy(&buf).to_string();
                    }
                    if stdout.len() > 100_000 { stdout.truncate(100_000); stdout.push_str("\n[...truncated]"); }
                    let _ = tx.send(CodeResult { stdout, stderr, exit_code: status.code().unwrap_or(-1), timed_out: false });
                    return;
                }
                Ok(None) => {}
                Err(e) => {
                    let _ = child.kill(); let _ = child.wait();
                    let _ = tx.send(CodeResult { stdout: String::new(), stderr: format!("Process died: {}", e), exit_code: -1, timed_out: false });
                    return;
                }
            }
            if start.elapsed() > Duration::from_secs(time_limit_secs) {
                let _ = child.kill(); let _ = child.wait();
                let _ = tx.send(CodeResult { stdout: String::new(), stderr: format!("Stopped after {}s. Split into smaller steps.", time_limit_secs), exit_code: 124, timed_out: true });
                return;
            }
            thread::sleep(Duration::from_millis(100));
        }
    });

    rx.recv().unwrap_or(CodeResult { stdout: String::new(), stderr: "Execution lost.".to_string(), exit_code: -1, timed_out: false })
}