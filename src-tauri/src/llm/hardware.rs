use serde::Serialize;
use std::process::Command;

#[derive(Serialize)]
pub struct HardwareInfo {
    pub os: String,
    pub cpu_cores: u32,
    pub total_ram_mb: u64,
    pub gpu_name: String,
    pub gpu_vram_mb: u64,
    pub recommended_model: String,
}

pub fn detect() -> HardwareInfo {
    let os = std::env::consts::OS.to_string();
    let cpu = num_cpus::get() as u32;
    let ram = total_ram_mb();

    let (gpu_name, gpu_vram) = detect_gpu();

    let recommended = if gpu_vram >= 8000 {
        "llama-3.2-3b-instruct-q4_k_m".to_string()
    } else if ram >= 8000 {
        "llama-3.2-1b-instruct-q4_k_m".to_string()
    } else {
        "tinyllama-1.1b-q4_k_m".to_string()
    };

    HardwareInfo {
        os,
        cpu_cores: cpu,
        total_ram_mb: ram,
        gpu_name,
        gpu_vram_mb: gpu_vram,
        recommended_model: recommended,
    }
}

fn total_ram_mb() -> u64 {
    if cfg!(target_os = "windows") {
        if let Ok(output) = Command::new("wmic").args(["ComputerSystem", "get", "TotalPhysicalMemory"]).output() {
            let s = String::from_utf8_lossy(&output.stdout);
            if let Some(line) = s.lines().nth(1) {
                if let Ok(bytes) = line.trim().parse::<u64>() {
                    return bytes / (1024 * 1024);
                }
            }
        }
    } else {
        if let Ok(output) = Command::new("sysctl").args(["-n", "hw.memsize"]).output() {
            if let Ok(s) = String::from_utf8(output.stdout) {
                if let Ok(bytes) = s.trim().parse::<u64>() {
                    return bytes / (1024 * 1024);
                }
            }
        }
        if let Ok(s) = std::fs::read_to_string("/proc/meminfo") {
            for line in s.lines() {
                if line.starts_with("MemTotal:") {
                    let kb: u64 = line.split_whitespace().nth(1).unwrap_or("0").parse().unwrap_or(0);
                    return kb / 1024;
                }
            }
        }
    }
    4096
}

fn detect_gpu() -> (String, u64) {
    let (name, vram) = if cfg!(target_os = "windows") {
        detect_nvidia_windows().unwrap_or_else(|| ("Unknown GPU".to_string(), 0))
    } else if cfg!(target_os = "macos") {
        detect_apple_gpu()
    } else {
        detect_nvidia_linux().unwrap_or_else(|| ("Unknown GPU".to_string(), 0))
    };
    (name, vram)
}

fn detect_nvidia_windows() -> Option<(String, u64)> {
    let output = Command::new("wmic")
        .args(["path", "win32_VideoController", "get", "name,AdapterRAM"])
        .output()
        .ok()?;
    let s = String::from_utf8_lossy(&output.stdout);
    for line in s.lines().skip(1) {
        let trimmed = line.trim();
        if trimmed.is_empty() { continue; }
        let parts: Vec<&str> = trimmed.rsplitn(2, ' ').collect();
        if parts.len() >= 2 {
            let vram_bytes: u64 = parts[0].parse().unwrap_or(0);
            let gpu = parts[1].trim().to_string();
            if gpu.to_lowercase().contains("nvidia") || gpu.to_lowercase().contains("amd") {
                return Some((gpu, vram_bytes / (1024 * 1024)));
            }
        }
    }
    None
}

fn detect_apple_gpu() -> (String, u64) {
    if let Ok(output) = Command::new("system_profiler").args(["SPDisplaysDataType"]).output() {
        let s = String::from_utf8_lossy(&output.stdout);
        let mut name = "Apple GPU".to_string();
        for line in s.lines() {
            let t = line.trim();
            if t.contains("Chipset Model:") {
                name = t.replace("Chipset Model:", "").trim().to_string();
            }
        }
        return (name, total_ram_mb() / 2);
    }
    ("Apple GPU".to_string(), total_ram_mb() / 2)
}

fn detect_nvidia_linux() -> Option<(String, u64)> {
    if let Ok(output) = Command::new("nvidia-smi").args(["--query-gpu=name,memory.total", "--format=csv,noheader"]).output() {
        let s = String::from_utf8_lossy(&output.stdout);
        for line in s.lines() {
            let parts: Vec<&str> = line.split(',').collect();
            if parts.len() >= 2 {
                let name = parts[0].trim().to_string();
                let vram_str = parts[1].trim().replace(" MiB", "");
                if let Ok(vram) = vram_str.parse::<u64>() {
                    return Some((name, vram));
                }
            }
        }
    }
    None
}