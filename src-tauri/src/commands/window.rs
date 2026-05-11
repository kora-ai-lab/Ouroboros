use tauri::{AppHandle, Manager};

#[tauri::command]
pub fn set_view_size(app: AppHandle, expanded: bool) -> Result<(), String> {
    let main = app.get_webview_window("main").ok_or("main window not found")?;

    if let Ok(monitors) = main.available_monitors() {
        if let Some(monitor) = monitors.into_iter().next() {
            let size = monitor.size();
            let pos = monitor.position();

            if expanded {
                main.set_size(tauri::LogicalSize::new(520.0, 680.0)).ok();
                main.set_position(tauri::PhysicalPosition::new(
                    pos.x + size.width as i32 / 2 - 260,
                    pos.y + size.height as i32 / 2 - 340,
                )).ok();
            } else {
                main.set_size(tauri::LogicalSize::new(480.0, 720.0)).ok();
                main.set_position(tauri::PhysicalPosition::new(
                    pos.x + size.width as i32 - 480,
                    pos.y + size.height as i32 - 720,
                )).ok();
            }
        }
    }

    Ok(())
}