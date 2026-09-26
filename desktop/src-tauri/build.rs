fn main() {
    let manifest = tauri_build::AppManifest::new().commands(&[
        "startup_status",
        "retry_service",
        "open_logs",
        "reconnect_content_root",
        "ensure_content_root",
        "pick_folder",
        "exit_for_update",
    ]);
    tauri_build::try_build(tauri_build::Attributes::new().app_manifest(manifest))
        .expect("failed to build PhotoAI desktop application")
}
