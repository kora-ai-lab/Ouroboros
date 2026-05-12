pub mod migrations;

use rusqlite::Connection;
use std::path::PathBuf;
use std::sync::Mutex;
use tauri::AppHandle;
use tauri::Manager;

pub struct Database {
    pub conn: Mutex<Connection>,
}

impl Database {
    pub fn new(app_handle: &AppHandle) -> Result<Self, Box<dyn std::error::Error>> {
        let app_dir = app_handle
            .path()
            .app_data_dir()
            .expect("failed to resolve app data dir");

        std::fs::create_dir_all(&app_dir)?;

        let db_path: PathBuf = app_dir.join("ouroboros.db");
        log::info!("Database path: {:?}", db_path);

        let conn = Connection::open(&db_path)?;
        conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")?;

        let db = Database {
            conn: Mutex::new(conn),
        };
        db.run_migrations()?;

        Ok(db)
    }

    pub fn run_migrations(&self) -> Result<(), Box<dyn std::error::Error>> {
        let conn = self.conn.lock().unwrap();
        let current_version: i32 = conn
            .query_row(
                "SELECT COALESCE(MAX(version), 0) FROM schema_version",
                [],
                |row| row.get(0),
            )
            .unwrap_or(0);

        if current_version < 1 {
            migrations::v1::apply(&conn)?;
        }
        if current_version < 2 {
            migrations::v2::apply(&conn)?;
        }
        if current_version < 3 {
            migrations::v3::apply(&conn)?;
        }
        if current_version < 4 {
            migrations::v4::apply(&conn)?;
        }
        if current_version < 5 {
            migrations::v5::apply(&conn)?;
        }

        Ok(())
    }

    pub fn conn(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().unwrap()
    }
}
