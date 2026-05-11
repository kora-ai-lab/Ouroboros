mod commands;
mod crypto;
mod db;
mod events;
mod llm;
mod sandbox;
mod state;

use state::AppState;
use commands::{window, ping, chat, tools, auth, models, onboarding, settings};
use tauri::{Manager, Emitter};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Shortcut};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_fs::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            let database = db::Database::new(app.handle())
                .expect("failed to initialize database");

            {
                let conn = database.conn();
                let completed: bool = conn
                    .query_row(
                        "SELECT value_json FROM settings WHERE key = ?1",
                        ["onboarding_completed"],
                        |row| row.get::<_, String>(0),
                    )
                    .ok()
                    .is_some();
                drop(conn);

                if !completed {
                    if let Some(main) = app.get_webview_window("main") {
                        main.show().ok();
                    }
                    tauri::WebviewWindowBuilder::new(
                        app,
                        "onboarding",
                        tauri::WebviewUrl::App("index.html?onboarding=true".into()),
                    )
                    .title("Ouroboros Setup")
                    .inner_size(500.0, 420.0)
                    .resizable(false)
                    .center()
                    .decorations(false)
                    .always_on_top(true)
                    .build()
                    .expect("failed to create onboarding window");
                } else {
                    if let Some(main) = app.get_webview_window("main") {
                        main.show().ok();
                        main.set_focus().ok();
                    }
                }
            }

            app.manage(AppState { db: database });

            if let Some(main) = app.get_webview_window("main") {
                if let Ok(monitors) = main.available_monitors() {
                    if let Some(monitor) = monitors.into_iter().next() {
                        let size = monitor.size();
                        let pos = monitor.position();
                        main.set_position(tauri::PhysicalPosition::new(
                            pos.x + size.width as i32 - 480,
                            pos.y + size.height as i32 - 720,
                        )).ok();
                    }
                }
            }

            app.handle().global_shortcut().on_shortcut(
                Shortcut::new(None, Code::F11),
                move |app, _shortcut, _event| {
                    if let Some(w) = app.get_webview_window("main") {
                        w.show().ok();
                        w.set_focus().ok();
                    }
                    app.emit("cycle-view", ()).ok();
                },
            )?;

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            window::set_view_size,
            ping::ping,
            chat::send_message,
            chat::get_conversations,
            chat::get_messages,
            chat::delete_conversation,
            chat::update_conversation_title,
            tools::execute_code,
            tools::list_tools,
            tools::save_tool,
            tools::delete_tool,
            auth::add_api_key,
            auth::remove_api_key,
            auth::list_providers,
            onboarding::complete_onboarding,
            models::get_hardware_info,
            models::list_models,
            settings::get_setting,
            settings::set_setting,
            settings::get_all_settings,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}