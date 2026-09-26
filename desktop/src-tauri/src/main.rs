#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

#[cfg(not(target_os = "windows"))]
compile_error!("照片选片桌面版首发仅支持 Windows x64。");

use serde::{Deserialize, Serialize};
use std::{
    ffi::OsStr,
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Write},
    net::{IpAddr, Ipv4Addr, SocketAddr, TcpStream},
    os::windows::{ffi::OsStrExt, fs::MetadataExt, process::CommandExt},
    path::{Component, Path, PathBuf, Prefix},
    process::{Child, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc, Arc, Mutex, RwLock,
    },
    time::{Duration, Instant},
};
use tauri::{
    AppHandle, Manager, RunEvent, State, Url, WebviewUrl, WebviewWindow, WebviewWindowBuilder,
    WindowEvent,
};
#[cfg(debug_assertions)]
use tauri_plugin_deep_link::DeepLinkExt;
use time::{format_description::well_known::Rfc3339, OffsetDateTime};
use url::Host;
use uuid::Uuid;
use windows_sys::Win32::Storage::FileSystem::{
    GetDiskFreeSpaceExW, GetDriveTypeW, GetLogicalDrives, GetVolumeInformationW, GetVolumePathNameW,
};
use windows_sys::Win32::System::WindowsProgramming::DRIVE_FIXED;
use winreg::{enums::HKEY_CURRENT_USER, RegKey};

const REGISTRY_KEY: &str = r"Software\PhotoAI";
const PENDING_MODEL_PROFILE_VALUE: &str = "PendingModelProfile";
const SERVICE_HANDSHAKE_PREFIX: &str = "PHOTO_AI_SERVICE/1 ";
const SERVICE_START_TIMEOUT: Duration = Duration::from_secs(15);
const SERVICE_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(10);
const SERVICE_CONTROL_IO_TIMEOUT: Duration = Duration::from_secs(2);
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
const BOOTSTRAP_DIRECTORY_NAME: &str = "PhotoAI-Bootstrap";
const BOOTSTRAP_MARKER_NAME: &str = ".photoai-bootstrap";
const BOOTSTRAP_MARKER_PREFIX: &str = "PHOTO_AI_BOOTSTRAP/1\n";
const UNTRUSTED_OLLAMA_ENVIRONMENT: &[&str] =
    &["PHOTO_AI_OLLAMA_ENDPOINT", "PHOTO_AI_OLLAMA", "OLLAMA_HOST"];
const CONTENT_DIRECTORIES: &[&str] = &[
    "state",
    "projects",
    "models",
    "runtimes",
    "tools",
    "styles",
    "cache",
    "downloads",
    "temp",
    "logs",
    "backups",
];

#[tauri::command]
fn exit_for_update(window: WebviewWindow, app: AppHandle, state: State<'_, DesktopState>) -> Result<(), String> {
    validate_service_window_origin(&window, &state)?;
    app.exit(0);
    Ok(())
}

struct DesktopState {
    content_root: Mutex<Option<PathBuf>>,
    bootstrap_root: Mutex<Option<PathBuf>>,
    content_root_configured: AtomicBool,
    service: Mutex<Option<Child>>,
    service_control: Mutex<Option<ServiceControl>>,
    starting: AtomicBool,
    status: Mutex<StartupStatus>,
    allowed_service_origin: Arc<RwLock<Option<String>>>,
}

impl Default for DesktopState {
    fn default() -> Self {
        Self {
            content_root: Mutex::new(None),
            bootstrap_root: Mutex::new(None),
            content_root_configured: AtomicBool::new(false),
            service: Mutex::new(None),
            service_control: Mutex::new(None),
            starting: AtomicBool::new(false),
            status: Mutex::new(StartupStatus::starting()),
            allowed_service_origin: Arc::new(RwLock::new(None)),
        }
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct StartupStatus {
    phase: String,
    message: Option<String>,
}

impl StartupStatus {
    fn starting() -> Self {
        Self {
            phase: "starting".into(),
            message: None,
        }
    }

    fn running() -> Self {
        Self {
            phase: "running".into(),
            message: None,
        }
    }

    fn error(message: impl Into<String>) -> Self {
        Self {
            phase: "error".into(),
            message: Some(message.into()),
        }
    }
}

#[derive(Deserialize)]
struct ServiceHandshake {
    port: u16,
    token: String,
    control_token: String,
    pid: u32,
    origin: String,
}

struct StartedService {
    child: Child,
    origin: String,
    bootstrap_token: String,
    control_token: String,
}

struct ServiceControl {
    origin: String,
    token: String,
}

#[derive(Serialize, Deserialize)]
struct ContentMarker {
    application: String,
    layout_version: u32,
    root_id: String,
    created_utc: String,
}

#[tauri::command]
fn startup_status(state: State<'_, DesktopState>) -> Result<StartupStatus, String> {
    state
        .status
        .lock()
        .map(|status| status.clone())
        .map_err(|_| "无法读取启动状态。".to_string())
}

#[tauri::command]
async fn retry_service(app: AppHandle) -> Result<(), String> {
    start_service(app).await
}

#[tauri::command]
fn open_logs(state: State<'_, DesktopState>) -> Result<(), String> {
    let root = state
        .content_root
        .lock()
        .map_err(|_| "无法读取数据目录。")?
        .clone()
        .ok_or("尚未配置数据目录。")?;
    let logs = root.join("logs");
    fs::create_dir_all(&logs).map_err(|error| format!("无法创建日志目录：{error}"))?;
    Command::new("explorer.exe")
        .arg(&logs)
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map_err(|error| format!("无法打开日志目录：{error}"))?;
    Ok(())
}

#[tauri::command]
fn reconnect_content_root(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, DesktopState>,
) -> Result<(), String> {
    validate_service_window_origin(&window, &state)?;
    let selected = pick_content_root(true)?.ok_or("已取消重新连接。")?;
    initialize_content_root(&selected)?;
    save_content_root(&selected)?;
    app.state::<DesktopState>()
        .content_root_configured
        .store(true, Ordering::Release);
    stop_service(&app);
    app.restart()
}

#[tauri::command]
fn ensure_content_root(
    window: WebviewWindow,
    app: AppHandle,
    state: State<'_, DesktopState>,
    profile_id: Option<String>,
) -> Result<bool, String> {
    validate_service_window_origin(&window, &state)?;
    if state.content_root_configured.load(Ordering::Acquire) {
        return Ok(true);
    }

    let profile_id = profile_id
        .map(|value| value.trim().to_ascii_lowercase())
        .filter(|value| !value.is_empty());
    if profile_id
        .as_deref()
        .is_some_and(|value| !matches!(value, "8gb" | "16gb"))
    {
        return Err("模型配置档位无效。".into());
    }

    let selected = match pick_content_root(false)? {
        Some(path) => path,
        None => return Ok(false),
    };
    let bootstrap = state
        .bootstrap_root
        .lock()
        .map_err(|_| "临时数据目录状态锁已损坏。")?
        .clone();
    if bootstrap
        .as_deref()
        .is_some_and(|path| paths_overlap(path, &selected))
    {
        return Err("不能把临时启动目录设为正式数据目录，请选择其他本机磁盘文件夹。".into());
    }

    save_content_root(&selected)?;
    if let Some(profile_id) = profile_id.as_deref() {
        save_pending_model_profile(profile_id)?;
    }
    state.content_root_configured.store(true, Ordering::Release);
    stop_service(&app);
    app.restart()
}

#[tauri::command]
async fn pick_folder(
    window: WebviewWindow,
    state: State<'_, DesktopState>,
    initial_directory: Option<String>,
) -> Result<Option<String>, String> {
    validate_service_window_origin(&window, &state)?;

    let initial = initial_directory
        .map(|value| value.trim().to_string())
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .filter(|path| path.is_dir());
    tauri::async_runtime::spawn_blocking(move || {
        let mut dialog = rfd::FileDialog::new().set_title("选择照片文件夹");
        if let Some(path) = initial {
            dialog = dialog.set_directory(path);
        }
        Ok(dialog
            .pick_folder()
            .map(canonical_or_original)
            .map(|path| path.to_string_lossy().into_owned()))
    })
    .await
    .map_err(|error| format!("原生文件夹选择任务异常：{error}"))?
}

#[tauri::command]
async fn pick_offline_bundle(
    window: WebviewWindow,
    state: State<'_, DesktopState>,
) -> Result<Option<String>, String> {
    validate_service_window_origin(&window, &state)?;
    tauri::async_runtime::spawn_blocking(move || {
        Ok(rfd::FileDialog::new()
            .set_title("选择 PhotoAI 16GB 离线资源包")
            .add_filter("PhotoAI 离线资源包", &["photoai-offline"])
            .pick_file()
            .map(canonical_or_original)
            .map(|path| path.to_string_lossy().into_owned()))
    })
    .await
    .map_err(|error| format!("原生离线包选择任务异常：{error}"))?
}

fn main() {
    if std::env::args_os().any(|argument| argument == "--self-test") {
        let result = std::env::current_exe()
            .map_err(|error| format!("无法定位 PhotoAI.exe：{error}"))
            .and_then(|path| {
                let resource_root = path
                    .parent()
                    .ok_or_else(|| "程序资源目录无效。".to_string())?;
                run_desktop_self_test(resource_root)
            });
        match result {
            Ok(()) => std::process::exit(0),
            Err(error) => {
                eprintln!("PhotoAI self-test failed: {error}");
                std::process::exit(1);
            }
        }
    }

    let app = tauri::Builder::default()
        // Tauri requires the single-instance plug-in to be registered first.
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            // A second ordinary launch and photoai://open both activate the
            // already running window. Payload-bearing links are not accepted.
            let _links_are_safe = args
                .iter()
                .filter(|argument| argument.contains("://"))
                .all(|argument| is_allowed_deep_link(argument));
            focus_main_window(app);
        }))
        .plugin(tauri_plugin_deep_link::init())
        .manage(DesktopState::default())
        .invoke_handler(tauri::generate_handler![
            startup_status,
            retry_service,
            open_logs,
            reconnect_content_root,
            ensure_content_root,
            pick_folder,
            pick_offline_bundle,
            exit_for_update
        ])
        .setup(|app| {
            #[cfg(debug_assertions)]
            app.deep_link().register_all()?;

            cleanup_stale_bootstrap_roots();
            let stored_root = load_content_root();
            let (root, configured, bootstrap_root) = match stored_root {
                Some(path) if validate_existing_content_root(&path).is_ok() => (path, true, None),
                Some(_) => match pick_content_root(true)? {
                    Some(path) => (path, true, None),
                    None => {
                        app.handle().exit(0);
                        return Ok(());
                    }
                },
                None => (default_content_root()?, true, None),
            };

            initialize_content_root(&root)?;
            if configured {
                save_content_root(&root)?;
            }
            {
                let state = app.state::<DesktopState>();
                *state
                    .content_root
                    .lock()
                    .map_err(|_| "数据目录状态锁已损坏。")? = Some(root.clone());
                *state
                    .bootstrap_root
                    .lock()
                    .map_err(|_| "临时数据目录状态锁已损坏。")? = bootstrap_root;
                state
                    .content_root_configured
                    .store(configured, Ordering::Release);
            }

            let allowed_origin = app.state::<DesktopState>().allowed_service_origin.clone();
            let webview_data = root.join("cache").join("webview2");
            fs::create_dir_all(&webview_data)?;

            let window =
                WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                    .title("照片选片")
                    .inner_size(1280.0, 820.0)
                    .min_inner_size(900.0, 620.0)
                    .visible(false)
                    .data_directory(webview_data)
                    .on_navigation(move |url| navigation_is_allowed(url, &allowed_origin))
                    .build()?;
            window.center()?;
            window.show()?;

            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                if let Err(error) = start_service(handle.clone()).await {
                    set_startup_status(&handle, StartupStatus::error(error));
                }
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("failed to build PhotoAI desktop application");

    app.run(|app, event| match event {
        RunEvent::ExitRequested { .. } | RunEvent::Exit => {
            stop_service(app);
            cleanup_bootstrap_content_root(app);
        }
        RunEvent::WindowEvent {
            event: WindowEvent::Destroyed,
            ..
        } => {
            if app.webview_windows().is_empty() {
                stop_service(app);
                cleanup_bootstrap_content_root(app);
                app.exit(0);
            }
        }
        _ => {}
    });
}

fn run_desktop_self_test(resource_root: &Path) -> Result<(), String> {
    let service = resource_root
        .join("core")
        .join("service")
        .join("PhotoAI.Service.exe");
    let worker = resource_root
        .join("core")
        .join("worker")
        .join("PhotoAI.CoreWorker.exe");
    let required = vec![
        service.clone(),
        worker.clone(),
        resource_root.join("tools").join("uv.exe"),
        resource_root.join("manifests").join("ai-requirements.lock"),
        resource_root.join("manifests").join("model-manifest.json"),
        resource_root
            .join("integrations")
            .join("photo-ai-lightroom.lrplugin")
            .join("Info.lua"),
    ];
    for path in required {
        if !path.is_file() {
            return Err(format!("安装资源缺失：{}", path.display()));
        }
    }
    let worker_wheels = resource_root.join("manifests").join("ai-worker");
    let wheel_count = fs::read_dir(&worker_wheels)
        .map_err(|error| format!("无法读取 AI Worker 资源：{error}"))?
        .filter_map(Result::ok)
        .filter(|entry| entry.path().extension() == Some(OsStr::new("whl")))
        .count();
    if wheel_count != 1 {
        return Err("AI Worker 资源必须恰好包含一个 wheel。".into());
    }
    run_sidecar_self_test(&service, SERVICE_HANDSHAKE_PREFIX.trim_end())?;
    run_sidecar_self_test(&worker, "PHOTO_AI_WORKER/1")?;
    Ok(())
}

fn run_sidecar_self_test(executable: &Path, expected_protocol: &str) -> Result<(), String> {
    let output = Command::new(executable)
        .arg("--self-test")
        .current_dir(executable.parent().ok_or("sidecar 目录无效。")?)
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map_err(|error| format!("无法运行 {} 自检：{error}", executable.display()))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    if !output.status.success()
        || !stdout.contains(expected_protocol)
        || !stdout.contains("\"status\":\"passed\"")
    {
        return Err(format!(
            "{} 自检失败（{}）：{}",
            executable.display(),
            output.status,
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(())
}

async fn start_service(app: AppHandle) -> Result<(), String> {
    let state = app.state::<DesktopState>();
    if state
        .starting
        .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
        .is_err()
    {
        return Err("内部服务正在启动，请稍候。".into());
    }

    set_startup_status(&app, StartupStatus::starting());
    let app_for_task = app.clone();
    let joined =
        tauri::async_runtime::spawn_blocking(move || launch_service_blocking(&app_for_task)).await;
    state.starting.store(false, Ordering::Release);
    let result = joined.map_err(|error| format!("内部服务启动任务异常：{error}"))?;

    let started = match result {
        Ok(started) => started,
        Err(error) => {
            set_startup_status(&app, StartupStatus::error(&error));
            return Err(error);
        }
    };

    let StartedService {
        child,
        origin,
        bootstrap_token,
        control_token,
    } = started;
    let mut destination =
        Url::parse(&format!("{origin}/")).map_err(|_| "内部服务返回了无效地址。".to_string())?;
    destination
        .query_pairs_mut()
        .append_pair("token", &bootstrap_token);
    let pending_profile = take_pending_model_profile();
    if let Some(profile_id) = pending_profile.as_deref() {
        destination
            .query_pairs_mut()
            .append_pair("install_profile", profile_id);
    }
    let service_pid = child.id();

    {
        let state = app.state::<DesktopState>();
        *state
            .allowed_service_origin
            .write()
            .map_err(|_| "导航安全状态锁已损坏。")? = Some(origin.clone());
        *state
            .service_control
            .lock()
            .map_err(|_| "服务控制状态锁已损坏。")? = Some(ServiceControl {
            origin,
            token: control_token,
        });
        *state.service.lock().map_err(|_| "服务状态锁已损坏。")? = Some(child);
    }

    let window = app.get_webview_window("main").ok_or("主窗口不存在。")?;
    if let Err(error) = window.navigate(destination) {
        if let Some(profile_id) = pending_profile.as_deref() {
            let _ = save_pending_model_profile(profile_id);
        }
        stop_service(&app);
        return Err(format!("无法打开内部界面：{error}"));
    }
    set_startup_status(&app, StartupStatus::running());
    monitor_service_exit(app, service_pid);
    Ok(())
}

fn monitor_service_exit(app: AppHandle, expected_pid: u32) {
    std::thread::spawn(move || loop {
        std::thread::sleep(Duration::from_secs(1));
        let state = app.state::<DesktopState>();
        let outcome = {
            let Ok(mut service) = state.service.lock() else {
                return;
            };
            let Some(child) = service.as_mut() else {
                return;
            };
            if child.id() != expected_pid {
                return;
            }
            match child.try_wait() {
                Ok(None) => None,
                Ok(Some(status)) => {
                    service.take();
                    Some(format!("内部服务已退出（{status}）。"))
                }
                Err(error) => {
                    service.take();
                    Some(format!("无法继续监控内部服务：{error}"))
                }
            }
        };

        if let Some(message) = outcome {
            clear_service_metadata(&state);
            set_startup_status(&app, StartupStatus::error(message));
            if let Some(window) = app.get_webview_window("main") {
                if let Ok(startup_page) = Url::parse("http://tauri.localhost/index.html") {
                    let _ = window.navigate(startup_page);
                }
            }
            return;
        }
    });
}

fn launch_service_blocking(app: &AppHandle) -> Result<StartedService, String> {
    let state = app.state::<DesktopState>();
    let stale_service_state = {
        let mut service = state.service.lock().map_err(|_| "服务状态锁已损坏。")?;
        if let Some(child) = service.as_mut() {
            match child.try_wait() {
                Ok(None) => return Err("内部服务已经在运行。".into()),
                Ok(Some(_)) | Err(_) => {
                    service.take();
                    true
                }
            }
        } else {
            false
        }
    };
    if stale_service_state {
        clear_service_metadata(&state);
    }

    let root = state
        .content_root
        .lock()
        .map_err(|_| "无法读取数据目录。")?
        .clone()
        .ok_or("尚未配置数据目录。")?;
    let service_exe = resolve_service_executable(app)?;
    let worker_exe = resolve_worker_executable(app)?;
    let resource_root = app
        .path()
        .resource_dir()
        .map_err(|error| format!("无法定位程序资源：{error}"))?;
    let service_dir = service_exe
        .parent()
        .ok_or("内部服务目录无效。")?
        .to_path_buf();

    let mut command = Command::new(&service_exe);
    command
        .arg("--port")
        .arg("0")
        .arg("--content-root")
        .arg(&root)
        .current_dir(service_dir)
        .env("PHOTO_AI_CONTENT_ROOT", &root)
        .env("PHOTO_AI_INSTALL_DIR", install_directory()?)
        .env("PHOTO_AI_DESKTOP_PID", std::process::id().to_string())
        .env("PHOTO_AI_CORE_WORKER_EXE", worker_exe)
        .env("PHOTO_AI_RESOURCE_ROOT", resource_root)
        .env("PHOTO_AI_PARENT_PID", std::process::id().to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .creation_flags(CREATE_NO_WINDOW);
    if !state.content_root_configured.load(Ordering::Acquire) {
        command
            .arg("--bootstrap-content-root")
            .env("PHOTO_AI_BOOTSTRAP_CONTENT_ROOT", "1");
    } else {
        command.env_remove("PHOTO_AI_BOOTSTRAP_CONTENT_ROOT");
    }
    isolate_service_environment(&mut command);
    let mut child = command
        .spawn()
        .map_err(|error| format!("无法启动内部服务：{error}"))?;
    let child_pid = child.id();
    let stdout = child.stdout.take().ok_or("无法读取内部服务握手。")?;
    let stderr = child.stderr.take().ok_or("无法读取内部服务错误输出。")?;
    let logs = root.join("logs");
    fs::create_dir_all(&logs).map_err(|error| format!("无法创建日志目录：{error}"))?;

    let (line_sender, line_receiver) = mpsc::channel();
    pump_process_output(
        stdout,
        logs.join("service.stdout.log"),
        Some(line_sender),
        true,
    );
    pump_process_output(stderr, logs.join("service.stderr.log"), None, false);

    let first_line = match line_receiver.recv_timeout(SERVICE_START_TIMEOUT) {
        Ok(Ok(line)) => line,
        Ok(Err(error)) => {
            terminate_child(&mut child);
            return Err(format!("读取内部服务握手失败：{error}"));
        }
        Err(mpsc::RecvTimeoutError::Timeout) => {
            terminate_child(&mut child);
            return Err("内部服务在 15 秒内没有完成启动。".into());
        }
        Err(mpsc::RecvTimeoutError::Disconnected) => {
            terminate_child(&mut child);
            return Err("内部服务在握手前退出。".into());
        }
    };

    let handshake = match parse_handshake(&first_line, child_pid) {
        Ok(value) => value,
        Err(error) => {
            terminate_child(&mut child);
            return Err(error);
        }
    };

    Ok(StartedService {
        child,
        origin: handshake.origin.trim_end_matches('/').to_string(),
        bootstrap_token: handshake.token,
        control_token: handshake.control_token,
    })
}

fn isolate_service_environment(command: &mut Command) {
    // The installed application owns its portable Ollama and assigns a fresh
    // loopback endpoint per worker.  Never inherit a developer/system Ollama
    // endpoint into the hidden Service process.
    for name in UNTRUSTED_OLLAMA_ENVIRONMENT {
        command.env_remove(name);
    }
}

fn parse_handshake(line: &str, expected_pid: u32) -> Result<ServiceHandshake, String> {
    let payload = line
        .strip_prefix(SERVICE_HANDSHAKE_PREFIX)
        .ok_or("内部服务没有返回受支持的 PHOTO_AI_SERVICE/1 握手。")?;
    let handshake: ServiceHandshake =
        serde_json::from_str(payload).map_err(|_| "内部服务握手格式无效。")?;
    if handshake.port == 0
        || handshake.pid != expected_pid
        || !valid_secret(&handshake.token)
        || !valid_secret(&handshake.control_token)
        || handshake.token == handshake.control_token
    {
        return Err("内部服务握手校验失败。".into());
    }

    let origin = Url::parse(&handshake.origin).map_err(|_| "内部服务来源地址无效。")?;
    let loopback = matches!(origin.host(), Some(Host::Ipv4(address)) if address.is_loopback());
    if origin.scheme() != "http"
        || !loopback
        || origin.port() != Some(handshake.port)
        || origin.username() != ""
        || origin.password().is_some()
        || origin.query().is_some()
        || origin.fragment().is_some()
        || origin.path() != "/"
    {
        return Err("内部服务没有绑定到预期的随机回环地址。".into());
    }
    Ok(handshake)
}

fn valid_secret(value: &str) -> bool {
    (32..=256).contains(&value.len())
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn pump_process_output<R>(
    reader: R,
    log_path: PathBuf,
    sender: Option<mpsc::Sender<Result<String, String>>>,
    redact_handshake: bool,
) where
    R: std::io::Read + Send + 'static,
{
    std::thread::spawn(move || {
        let mut log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(log_path)
            .ok();
        for line in BufReader::new(reader).lines() {
            match line {
                Ok(line) => {
                    if !redact_handshake || !line.contains(SERVICE_HANDSHAKE_PREFIX.trim_end()) {
                        if let Some(file) = log.as_mut() {
                            let _ = writeln!(file, "{line}");
                        }
                    }
                    if let Some(channel) = sender.as_ref() {
                        let _ = channel.send(Ok(line));
                    }
                }
                Err(error) => {
                    if let Some(channel) = sender.as_ref() {
                        let _ = channel.send(Err(error.to_string()));
                    }
                    break;
                }
            }
        }
    });
}

fn resolve_service_executable(app: &AppHandle) -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    if let Some(path) = std::env::var_os("PHOTO_AI_SERVICE_EXE").map(PathBuf::from) {
        if path.is_file() {
            return Ok(path);
        }
    }

    let path = app
        .path()
        .resource_dir()
        .map_err(|error| format!("无法定位程序资源：{error}"))?
        .join("core")
        .join("service")
        .join("PhotoAI.Service.exe");
    if !path.is_file() {
        return Err("安装内容不完整：缺少 PhotoAI.Service.exe。".into());
    }
    Ok(path)
}

fn resolve_worker_executable(app: &AppHandle) -> Result<PathBuf, String> {
    #[cfg(debug_assertions)]
    if let Some(path) = std::env::var_os("PHOTO_AI_CORE_WORKER_EXE").map(PathBuf::from) {
        if path.is_file() {
            return Ok(path);
        }
    }

    let path = app
        .path()
        .resource_dir()
        .map_err(|error| format!("无法定位程序资源：{error}"))?
        .join("core")
        .join("worker")
        .join("PhotoAI.CoreWorker.exe");
    if !path.is_file() {
        return Err("安装内容不完整：缺少 PhotoAI.CoreWorker.exe。".into());
    }
    Ok(path)
}

fn set_startup_status(app: &AppHandle, status: StartupStatus) {
    if let Ok(mut current) = app.state::<DesktopState>().status.lock() {
        *current = status;
    }
}

fn stop_service(app: &AppHandle) {
    if let Some(state) = app.try_state::<DesktopState>() {
        let control = clear_service_metadata(&state);
        let mut service = match state.service.lock() {
            Ok(service) => service,
            Err(poisoned) => poisoned.into_inner(),
        };
        if let Some(mut child) = service.take() {
            let graceful_requested = control
                .as_ref()
                .map(request_service_shutdown)
                .unwrap_or(false);
            let timeout = if graceful_requested {
                SERVICE_SHUTDOWN_TIMEOUT
            } else {
                SERVICE_CONTROL_IO_TIMEOUT
            };
            if !wait_for_child_exit(&mut child, timeout) {
                terminate_child(&mut child);
            }
        }
    }
}

fn clear_service_metadata(state: &DesktopState) -> Option<ServiceControl> {
    state.starting.store(false, Ordering::Release);
    match state.allowed_service_origin.write() {
        Ok(mut origin) => {
            origin.take();
        }
        Err(poisoned) => {
            poisoned.into_inner().take();
        }
    }
    match state.service_control.lock() {
        Ok(mut control) => control.take(),
        Err(poisoned) => poisoned.into_inner().take(),
    }
}

fn request_service_shutdown(control: &ServiceControl) -> bool {
    let Some((address, request)) = build_shutdown_request(&control.origin, &control.token) else {
        return false;
    };
    let Ok(mut stream) = TcpStream::connect_timeout(&address, SERVICE_CONTROL_IO_TIMEOUT) else {
        return false;
    };
    if stream
        .set_write_timeout(Some(SERVICE_CONTROL_IO_TIMEOUT))
        .is_err()
        || stream
            .set_read_timeout(Some(SERVICE_CONTROL_IO_TIMEOUT))
            .is_err()
        || stream.write_all(&request).is_err()
        || stream.flush().is_err()
    {
        return false;
    }

    let mut status_line = String::new();
    if BufReader::new(stream).read_line(&mut status_line).is_err() {
        return false;
    }
    matches!(status_line.split_ascii_whitespace().nth(1), Some("200"))
}

fn build_shutdown_request(origin: &str, token: &str) -> Option<(SocketAddr, Vec<u8>)> {
    if !valid_secret(token) {
        return None;
    }
    let parsed = Url::parse(origin).ok()?;
    let address = match parsed.host()? {
        Host::Ipv4(address) if address.is_loopback() => address,
        _ => return None,
    };
    if parsed.scheme() != "http"
        || parsed.path() != "/"
        || parsed.query().is_some()
        || parsed.fragment().is_some()
        || parsed.username() != ""
        || parsed.password().is_some()
    {
        return None;
    }
    let port = parsed.port()?;
    let socket = SocketAddr::new(IpAddr::V4(Ipv4Addr::from(address.octets())), port);
    let request = format!(
        "POST /api/service/shutdown HTTP/1.1\r\nHost: {address}:{port}\r\nAuthorization: Bearer {token}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
    )
    .into_bytes();
    Some((socket, request))
}

fn wait_for_child_exit(child: &mut Child, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Ok(None) if Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(50));
            }
            Ok(None) | Err(_) => return false,
        }
    }
}

fn terminate_child(child: &mut Child) {
    let pid = child.id().to_string();
    let _ = Command::new("taskkill.exe")
        .args(["/PID", &pid, "/T", "/F"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .creation_flags(CREATE_NO_WINDOW)
        .status();
    if wait_for_child_exit(child, SERVICE_CONTROL_IO_TIMEOUT) {
        return;
    }
    let _ = child.kill();
    let _ = child.wait();
}

fn focus_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn is_allowed_deep_link(argument: &str) -> bool {
    Url::parse(argument)
        .map(|url| url.scheme() == "photoai" && url.host_str() == Some("open"))
        .unwrap_or(false)
}

fn navigation_is_allowed(url: &Url, service_origin: &Arc<RwLock<Option<String>>>) -> bool {
    let local_page = url.scheme() == "tauri"
        || ((url.scheme() == "http" || url.scheme() == "https")
            && url.host_str() == Some("tauri.localhost"));
    if local_page {
        return true;
    }
    let expected = service_origin.read().ok().and_then(|value| value.clone());
    expected
        .map(|origin| url.origin().ascii_serialization() == origin)
        .unwrap_or(false)
}

fn folder_picker_origin_is_allowed(label: &str, url: &Url, expected_origin: Option<&str>) -> bool {
    label == "main"
        && expected_origin
            .map(|origin| url.origin().ascii_serialization() == origin)
            .unwrap_or(false)
}

fn validate_service_window_origin(
    window: &WebviewWindow,
    state: &State<'_, DesktopState>,
) -> Result<(), String> {
    let current_url = window
        .url()
        .map_err(|error| format!("无法验证当前桌面页面：{error}"))?;
    let expected_origin = state
        .allowed_service_origin
        .read()
        .map_err(|_| "导航安全状态锁已损坏。")?
        .clone();
    if !folder_picker_origin_is_allowed(window.label(), &current_url, expected_origin.as_deref()) {
        return Err("原生目录操作仅供当前桌面程序使用。".into());
    }
    Ok(())
}

fn load_content_root() -> Option<PathBuf> {
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    let key = hkcu.open_subkey(REGISTRY_KEY).ok()?;
    let value: String = key.get_value("ContentRoot").ok()?;
    let trimmed = value.trim();
    (!trimmed.is_empty()).then(|| PathBuf::from(trimmed))
}

fn save_content_root(path: &Path) -> Result<(), String> {
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    let (key, _) = hkcu
        .create_subkey(REGISTRY_KEY)
        .map_err(|error| format!("无法保存数据目录：{error}"))?;
    key.set_value("ContentRoot", &path.to_string_lossy().as_ref())
        .map_err(|error| format!("无法保存数据目录：{error}"))?;
    key.set_value("InstallVersion", &env!("CARGO_PKG_VERSION"))
        .map_err(|error| format!("无法保存安装版本：{error}"))?;
    Ok(())
}

fn save_pending_model_profile(profile_id: &str) -> Result<(), String> {
    if !matches!(profile_id, "8gb" | "16gb") {
        return Err("模型配置档位无效。".into());
    }
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    let (key, _) = hkcu
        .create_subkey(REGISTRY_KEY)
        .map_err(|error| format!("无法保存待安装模型配置：{error}"))?;
    key.set_value(PENDING_MODEL_PROFILE_VALUE, &profile_id)
        .map_err(|error| format!("无法保存待安装模型配置：{error}"))
}

fn take_pending_model_profile() -> Option<String> {
    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    let key = hkcu.open_subkey_with_flags(
        REGISTRY_KEY,
        winreg::enums::KEY_READ | winreg::enums::KEY_WRITE,
    );
    let key = key.ok()?;
    let value: String = key.get_value(PENDING_MODEL_PROFILE_VALUE).ok()?;
    let _ = key.delete_value(PENDING_MODEL_PROFILE_VALUE);
    let normalized = value.trim().to_ascii_lowercase();
    matches!(normalized.as_str(), "8gb" | "16gb").then_some(normalized)
}

fn pick_content_root(reconnect: bool) -> Result<Option<PathBuf>, String> {
    let recommendation = recommended_content_root();
    loop {
        let title = match recommendation.as_ref() {
            Some(path) if reconnect => {
                format!("重新连接照片选片数据目录（推荐位置：{}）", path.display())
            }
            Some(path) => format!("选择照片选片数据目录（推荐位置：{}）", path.display()),
            None if reconnect => "重新连接照片选片数据目录".to_string(),
            None => "选择照片选片数据目录".to_string(),
        };
        let mut dialog = rfd::FileDialog::new().set_title(&title);
        if let Some(parent) = recommendation.as_deref().and_then(Path::parent) {
            dialog = dialog.set_directory(parent);
        }
        let Some(selected) = dialog.pick_folder() else {
            return Ok(None);
        };
        match initialize_content_root(&selected) {
            Ok(()) => return Ok(Some(canonical_or_original(selected))),
            Err(error) => {
                rfd::MessageDialog::new()
                    .set_title("无法使用该目录")
                    .set_description(&error)
                    .set_level(rfd::MessageLevel::Error)
                    .set_buttons(rfd::MessageButtons::Ok)
                    .show();
            }
        }
    }
}

fn initialize_content_root(path: &Path) -> Result<(), String> {
    validate_content_root_location(path)?;
    reject_reparse_components(path)?;
    fs::create_dir_all(path).map_err(|error| format!("无法创建数据目录：{error}"))?;
    let canonical = normalize_verbatim_disk_path(
        fs::canonicalize(path).map_err(|error| format!("无法读取数据目录：{error}"))?,
    );
    validate_content_root_location(&canonical)?;
    validate_local_filesystem(&canonical)?;
    validate_not_install_directory(&canonical)?;

    let marker_path = canonical.join("marker.json");
    if marker_path.exists() {
        validate_marker(&marker_path)?;
    } else {
        let mut entries =
            fs::read_dir(&canonical).map_err(|error| format!("无法检查数据目录：{error}"))?;
        if entries.next().is_some() {
            return Err("请选择一个空目录，或选择原有的照片选片数据目录。".into());
        }
        write_marker(&canonical)?;
    }

    for name in CONTENT_DIRECTORIES {
        fs::create_dir_all(canonical.join(name))
            .map_err(|error| format!("无法创建 {name} 目录：{error}"))?;
    }
    verify_writable(&canonical)?;
    Ok(())
}

fn validate_existing_content_root(path: &Path) -> Result<(), String> {
    if !path.is_dir() {
        return Err("数据目录当前不可用。".into());
    }
    reject_reparse_components(path)?;
    let canonical =
        normalize_verbatim_disk_path(fs::canonicalize(path).map_err(|_| "数据目录当前不可用。")?);
    validate_content_root_location(&canonical)?;
    validate_local_filesystem(&canonical)?;
    validate_not_install_directory(&canonical)?;
    validate_marker(&canonical.join("marker.json"))?;
    verify_writable(&canonical)
}

fn validate_content_root_location(path: &Path) -> Result<(), String> {
    let network_prefix = matches!(
        path.components().next(),
        Some(Component::Prefix(prefix))
            if matches!(prefix.kind(), Prefix::UNC(_, _) | Prefix::VerbatimUNC(_, _))
    );
    if network_prefix {
        return Err("数据目录必须位于本机磁盘，不能使用 UNC 网络目录。".into());
    }
    if path.as_os_str().is_empty() {
        return Err("数据目录不能为空。".into());
    }
    Ok(())
}

fn install_directory() -> Result<PathBuf, String> {
    let executable =
        std::env::current_exe().map_err(|error| format!("无法读取安装路径：{error}"))?;
    executable
        .parent()
        .map(canonical_or_original)
        .ok_or_else(|| "无法确定程序安装目录。".to_string())
}

fn default_content_root() -> Result<PathBuf, String> {
    Ok(install_directory()?.join("data"))
}

fn validate_not_install_directory(path: &Path) -> Result<(), String> {
    let install_dir = install_directory()?;
    let path = canonical_or_original(path.to_path_buf());
    if path == install_dir || path_is_prefix(&path, &install_dir) {
        return Err("数据目录不能直接使用程序安装目录或它的上级目录。".into());
    }
    Ok(())
}

fn path_is_prefix(first: &Path, second: &Path) -> bool {
    fn components(path: &Path) -> Vec<String> {
        normalize_verbatim_disk_path(path.to_path_buf())
            .components()
            .map(|value| value.as_os_str().to_string_lossy().to_ascii_lowercase())
            .collect()
    }

    let first = components(first);
    let second = components(second);
    first.len() <= second.len() && first == second[..first.len()]
}

fn paths_overlap(first: &Path, second: &Path) -> bool {
    path_is_prefix(first, second) || path_is_prefix(second, first)
}

fn reject_reparse_components(path: &Path) -> Result<(), String> {
    for component in path.ancestors() {
        let metadata = match fs::symlink_metadata(component) {
            Ok(metadata) => metadata,
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => continue,
            Err(error) => return Err(format!("无法检查数据目录路径：{error}")),
        };
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(format!(
                "数据目录不能经过符号链接或目录联接点：{}",
                component.display()
            ));
        }
    }
    Ok(())
}

fn validate_local_filesystem(path: &Path) -> Result<(), String> {
    let volume_root = volume_root(path)?;
    if canonical_or_original(path.to_path_buf()) == canonical_or_original(volume_root.clone()) {
        return Err("请在磁盘中创建一个专用文件夹，不能直接使用盘符根目录。".into());
    }
    let root_wide = wide_null(volume_root.as_os_str());
    let drive_type = unsafe { GetDriveTypeW(root_wide.as_ptr()) };
    if drive_type != DRIVE_FIXED {
        return Err("数据目录必须位于本机固定磁盘。".into());
    }

    let mut filesystem = [0u16; 32];
    let success = unsafe {
        GetVolumeInformationW(
            root_wide.as_ptr(),
            std::ptr::null_mut(),
            0,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            std::ptr::null_mut(),
            filesystem.as_mut_ptr(),
            filesystem.len() as u32,
        )
    };
    if success == 0 {
        return Err("无法确认数据盘文件系统。".into());
    }
    let end = filesystem
        .iter()
        .position(|value| *value == 0)
        .unwrap_or(filesystem.len());
    let name = String::from_utf16_lossy(&filesystem[..end]).to_ascii_uppercase();
    if name != "NTFS" && name != "REFS" {
        return Err(format!("数据盘必须使用 NTFS 或 ReFS，当前为 {name}。"));
    }
    Ok(())
}

fn volume_root(path: &Path) -> Result<PathBuf, String> {
    let input = wide_null(path.as_os_str());
    let mut output = [0u16; 260];
    let success =
        unsafe { GetVolumePathNameW(input.as_ptr(), output.as_mut_ptr(), output.len() as u32) };
    if success == 0 {
        return Err("无法定位数据盘。".into());
    }
    let end = output
        .iter()
        .position(|value| *value == 0)
        .unwrap_or(output.len());
    Ok(PathBuf::from(String::from_utf16_lossy(&output[..end])))
}

fn verify_writable(path: &Path) -> Result<(), String> {
    let probe = path.join(format!(".photoai-write-test-{}", std::process::id()));
    let result = OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&probe)
        .and_then(|mut file| {
            file.write_all(b"photoai")?;
            file.sync_all()
        });
    let _ = fs::remove_file(&probe);
    result.map_err(|error| format!("数据目录不可写：{error}"))
}

fn validate_marker(path: &Path) -> Result<(), String> {
    let bytes = fs::read(path).map_err(|_| "所选目录不是有效的照片选片数据目录。")?;
    let marker: ContentMarker =
        serde_json::from_slice(&bytes).map_err(|_| "数据目录标记已损坏。")?;
    if marker.application != "PhotoAI"
        || marker.layout_version != 1
        || Uuid::parse_str(&marker.root_id).is_err()
        || OffsetDateTime::parse(&marker.created_utc, &Rfc3339).is_err()
    {
        return Err("数据目录标记版本或产品标识不匹配。".into());
    }
    Ok(())
}

fn write_marker(root: &Path) -> Result<(), String> {
    let marker = ContentMarker {
        application: "PhotoAI".into(),
        layout_version: 1,
        root_id: Uuid::new_v4().to_string(),
        created_utc: OffsetDateTime::now_utc()
            .format(&Rfc3339)
            .map_err(|error| format!("无法生成数据目录时间：{error}"))?,
    };
    let temporary = root.join("marker.json.tmp");
    let destination = root.join("marker.json");
    let bytes = serde_json::to_vec_pretty(&marker).map_err(|error| error.to_string())?;
    let mut file =
        File::create(&temporary).map_err(|error| format!("无法创建数据目录标记：{error}"))?;
    file.write_all(&bytes)
        .and_then(|_| file.sync_all())
        .map_err(|error| format!("无法写入数据目录标记：{error}"))?;
    fs::rename(&temporary, &destination)
        .map_err(|error| format!("无法发布数据目录标记：{error}"))?;
    Ok(())
}

fn bootstrap_base_path() -> PathBuf {
    std::env::temp_dir().join(BOOTSTRAP_DIRECTORY_NAME)
}

fn create_bootstrap_content_root() -> Result<PathBuf, String> {
    let base = bootstrap_base_path();
    fs::create_dir_all(&base).map_err(|error| format!("无法创建临时启动目录：{error}"))?;
    reject_reparse_components(&base)?;
    let root = base.join(format!("session-{}", Uuid::new_v4()));
    initialize_content_root(&root)?;

    let result = (|| {
        let marker_bytes = fs::read(root.join("marker.json"))
            .map_err(|error| format!("无法读取临时启动目录标记：{error}"))?;
        let marker: ContentMarker = serde_json::from_slice(&marker_bytes)
            .map_err(|error| format!("临时启动目录标记无效：{error}"))?;
        let payload = format!("{BOOTSTRAP_MARKER_PREFIX}{}\n", marker.root_id);
        let path = root.join(BOOTSTRAP_MARKER_NAME);
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&path)
            .map_err(|error| format!("无法创建临时启动目录所有权标记：{error}"))?;
        file.write_all(payload.as_bytes())
            .and_then(|_| file.sync_all())
            .map_err(|error| format!("无法写入临时启动目录所有权标记：{error}"))?;
        Ok(canonical_or_original(root.clone()))
    })();
    if result.is_err() && is_direct_bootstrap_session_path(&root) {
        let _ = fs::remove_dir_all(&root);
    }
    result
}

fn is_direct_bootstrap_session_path(path: &Path) -> bool {
    let Some(name) = path.file_name().and_then(OsStr::to_str) else {
        return false;
    };
    let Some(id) = name.strip_prefix("session-") else {
        return false;
    };
    if Uuid::parse_str(id).is_err() {
        return false;
    }
    let Some(parent) = path.parent() else {
        return false;
    };
    canonical_or_original(parent.to_path_buf()) == canonical_or_original(bootstrap_base_path())
}

fn bootstrap_root_is_owned(path: &Path) -> bool {
    if !path.is_dir() || !is_direct_bootstrap_session_path(path) {
        return false;
    }
    if reject_reparse_components(path).is_err() {
        return false;
    }
    let Ok(metadata) = fs::symlink_metadata(path) else {
        return false;
    };
    if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return false;
    }
    let marker_path = path.join("marker.json");
    if validate_marker(&marker_path).is_err() {
        return false;
    }
    let Ok(marker_bytes) = fs::read(marker_path) else {
        return false;
    };
    let Ok(marker) = serde_json::from_slice::<ContentMarker>(&marker_bytes) else {
        return false;
    };
    let expected = format!("{BOOTSTRAP_MARKER_PREFIX}{}\n", marker.root_id);
    fs::read_to_string(path.join(BOOTSTRAP_MARKER_NAME))
        .map(|value| value == expected)
        .unwrap_or(false)
}

fn cleanup_stale_bootstrap_roots() {
    let base = bootstrap_base_path();
    let Ok(entries) = fs::read_dir(&base) else {
        return;
    };
    for entry in entries.flatten() {
        let path = entry.path();
        if bootstrap_root_is_owned(&path) {
            let _ = fs::remove_dir_all(path);
        }
    }
    let _ = fs::remove_dir(&base);
}

fn cleanup_bootstrap_content_root(app: &AppHandle) {
    let Some(state) = app.try_state::<DesktopState>() else {
        return;
    };
    let root = match state.bootstrap_root.lock() {
        Ok(mut value) => value.take(),
        Err(poisoned) => poisoned.into_inner().take(),
    };
    if let Some(path) = root {
        if bootstrap_root_is_owned(&path) {
            let _ = fs::remove_dir_all(path);
        }
        let _ = fs::remove_dir(bootstrap_base_path());
    }
}

fn recommended_content_root() -> Option<PathBuf> {
    let system_drive = std::env::var("SystemDrive")
        .ok()
        .map(|value| value.trim_end_matches(['\\', '/']).to_ascii_uppercase());
    let drive_mask = unsafe { GetLogicalDrives() };
    let mut best: Option<(u64, PathBuf)> = None;
    for offset in 0..26u32 {
        if drive_mask & (1 << offset) == 0 {
            continue;
        }
        let letter = (b'A' + offset as u8) as char;
        let drive_name = format!("{letter}:");
        if system_drive.as_deref() == Some(&drive_name) {
            continue;
        }
        let root = PathBuf::from(format!(r"{letter}:\"));
        let root_wide = wide_null(root.as_os_str());
        if unsafe { GetDriveTypeW(root_wide.as_ptr()) } != DRIVE_FIXED {
            continue;
        }
        let mut available = 0u64;
        let ok = unsafe {
            GetDiskFreeSpaceExW(
                root_wide.as_ptr(),
                &mut available,
                std::ptr::null_mut(),
                std::ptr::null_mut(),
            )
        };
        if ok != 0
            && best
                .as_ref()
                .map(|(size, _)| available > *size)
                .unwrap_or(true)
        {
            best = Some((available, root.join("PhotoAI")));
        }
    }
    best.map(|(_, path)| path)
}

fn wide_null(value: &OsStr) -> Vec<u16> {
    value.encode_wide().chain(std::iter::once(0)).collect()
}

fn canonical_or_original(path: impl Into<PathBuf>) -> PathBuf {
    let path = path.into();
    normalize_verbatim_disk_path(fs::canonicalize(&path).unwrap_or(path))
}

fn normalize_verbatim_disk_path(path: PathBuf) -> PathBuf {
    let text = path.as_os_str().to_string_lossy();
    match text.strip_prefix(r"\\?\") {
        Some(rest) if !rest.to_ascii_uppercase().starts_with("UNC\\") => PathBuf::from(rest),
        _ => path,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_exact_loopback_handshake() {
        let pid = std::process::id();
        let line = format!(
            r#"PHOTO_AI_SERVICE/1 {{"port":49152,"token":"0123456789abcdef0123456789abcdef","control_token":"fedcba9876543210fedcba9876543210","pid":{pid},"origin":"http://127.0.0.1:49152"}}"#
        );
        let parsed = parse_handshake(&line, pid).expect("valid handshake");
        assert_eq!(parsed.port, 49152);
    }

    #[test]
    fn service_command_removes_inherited_ollama_environment() {
        let mut command = Command::new("PhotoAI.Service.exe");
        command
            .env("PHOTO_AI_OLLAMA_ENDPOINT", "http://127.0.0.1:11435")
            .env("PHOTO_AI_OLLAMA", r"C:\external\ollama.exe")
            .env("OLLAMA_HOST", "127.0.0.1:11435");

        isolate_service_environment(&mut command);

        let removed: Vec<_> = command
            .get_envs()
            .filter_map(|(name, value)| value.is_none().then_some(name.to_string_lossy()))
            .collect();
        for name in UNTRUSTED_OLLAMA_ENVIRONMENT {
            assert!(removed.iter().any(|removed_name| removed_name == name));
        }
    }

    #[test]
    fn rejects_non_loopback_handshake() {
        let pid = std::process::id();
        let line = format!(
            r#"PHOTO_AI_SERVICE/1 {{"port":49152,"token":"0123456789abcdef0123456789abcdef","control_token":"fedcba9876543210fedcba9876543210","pid":{pid},"origin":"http://192.168.1.2:49152"}}"#
        );
        assert!(parse_handshake(&line, pid).is_err());
    }

    #[test]
    fn rejects_missing_or_reused_control_token() {
        let pid = std::process::id();
        let missing = format!(
            r#"PHOTO_AI_SERVICE/1 {{"port":49152,"token":"0123456789abcdef0123456789abcdef","pid":{pid},"origin":"http://127.0.0.1:49152"}}"#
        );
        assert!(parse_handshake(&missing, pid).is_err());

        let reused = format!(
            r#"PHOTO_AI_SERVICE/1 {{"port":49152,"token":"0123456789abcdef0123456789abcdef","control_token":"0123456789abcdef0123456789abcdef","pid":{pid},"origin":"http://127.0.0.1:49152"}}"#
        );
        assert!(parse_handshake(&reused, pid).is_err());
    }

    #[test]
    fn shutdown_request_uses_control_bearer_without_url_disclosure() {
        use std::{io::Read, net::TcpListener, sync::mpsc::sync_channel};

        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let (sender, receiver) = sync_channel(1);
        let server = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(2)))
                .unwrap();
            let mut request = Vec::new();
            let mut chunk = [0u8; 512];
            while !request.windows(4).any(|window| window == b"\r\n\r\n") {
                let count = stream.read(&mut chunk).unwrap();
                if count == 0 {
                    break;
                }
                request.extend_from_slice(&chunk[..count]);
            }
            sender.send(String::from_utf8(request).unwrap()).unwrap();
            stream
                .write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                .unwrap();
        });

        let token = "control_secret_0123456789abcdef0123456789";
        assert!(request_service_shutdown(&ServiceControl {
            origin: format!("http://{address}"),
            token: token.into(),
        }));
        let request = receiver.recv_timeout(Duration::from_secs(2)).unwrap();
        assert!(request.starts_with("POST /api/service/shutdown HTTP/1.1\r\n"));
        assert!(request.contains(&format!("\r\nAuthorization: Bearer {token}\r\n")));
        assert!(!request.starts_with(&format!("POST /api/service/shutdown?token={token}")));
        server.join().unwrap();
    }

    #[test]
    fn clearing_service_metadata_removes_origin_and_control_secret() {
        let state = DesktopState::default();
        state.starting.store(true, Ordering::Release);
        *state.allowed_service_origin.write().unwrap() = Some("http://127.0.0.1:49152".into());
        *state.service_control.lock().unwrap() = Some(ServiceControl {
            origin: "http://127.0.0.1:49152".into(),
            token: "control_secret_0123456789abcdef0123456789".into(),
        });

        let taken = clear_service_metadata(&state).expect("control state");
        assert_eq!(taken.origin, "http://127.0.0.1:49152");
        assert!(state.service_control.lock().unwrap().is_none());
        assert!(state.allowed_service_origin.read().unwrap().is_none());
        assert!(!state.starting.load(Ordering::Acquire));
    }

    #[test]
    fn accepts_only_open_deep_link() {
        assert!(is_allowed_deep_link("photoai://open"));
        assert!(!is_allowed_deep_link("photoai://delete"));
        assert!(!is_allowed_deep_link("https://example.com"));
    }

    #[test]
    fn navigation_is_bound_to_exact_origin() {
        let allowed = Arc::new(RwLock::new(Some("http://127.0.0.1:49152".to_string())));
        assert!(navigation_is_allowed(
            &Url::parse("http://127.0.0.1:49152/#projects").unwrap(),
            &allowed
        ));
        assert!(!navigation_is_allowed(
            &Url::parse("http://127.0.0.1:49153/").unwrap(),
            &allowed
        ));
        assert!(!navigation_is_allowed(
            &Url::parse("https://example.com/").unwrap(),
            &allowed
        ));
    }

    #[test]
    fn folder_picker_is_bound_to_main_window_and_exact_service_origin() {
        let expected = Some("http://127.0.0.1:49152");
        assert!(folder_picker_origin_is_allowed(
            "main",
            &Url::parse("http://127.0.0.1:49152/#toolbox").unwrap(),
            expected
        ));
        assert!(!folder_picker_origin_is_allowed(
            "main",
            &Url::parse("http://127.0.0.1:49153/").unwrap(),
            expected
        ));
        assert!(!folder_picker_origin_is_allowed(
            "secondary",
            &Url::parse("http://127.0.0.1:49152/").unwrap(),
            expected
        ));
        assert!(!folder_picker_origin_is_allowed(
            "main",
            &Url::parse("http://127.0.0.1:49152/").unwrap(),
            None
        ));
    }

    #[test]
    fn distinguishes_local_verbatim_paths_from_unc_paths() {
        assert!(validate_content_root_location(Path::new(r"\\server\share\PhotoAI")).is_err());
        assert!(validate_content_root_location(Path::new(r"\\?\D:\PhotoAI")).is_ok());
    }

    #[test]
    fn marker_contract_has_only_shared_portable_fields() {
        let marker = ContentMarker {
            application: "PhotoAI".into(),
            layout_version: 1,
            root_id: "b7498148-69d3-47e4-80c5-86b138a3c6d5".into(),
            created_utc: "2026-09-02T09:00:00Z".into(),
        };
        let object = serde_json::to_value(marker)
            .unwrap()
            .as_object()
            .unwrap()
            .clone();
        assert_eq!(object.len(), 4);
        assert!(object.contains_key("application"));
        assert!(object.contains_key("layout_version"));
        assert!(object.contains_key("root_id"));
        assert!(object.contains_key("created_utc"));
    }

    #[test]
    fn temporary_bootstrap_root_requires_both_ownership_markers() {
        let root = create_bootstrap_content_root().expect("bootstrap root");
        assert!(bootstrap_root_is_owned(&root));
        fs::write(root.join(BOOTSTRAP_MARKER_NAME), "tampered").expect("tamper bootstrap marker");
        assert!(!bootstrap_root_is_owned(&root));
        assert!(is_direct_bootstrap_session_path(&root));
        fs::remove_dir_all(&root).expect("remove test bootstrap root");
        let _ = fs::remove_dir(bootstrap_base_path());
    }

    #[test]
    fn distinguishes_install_directory_ancestors_from_data_descendants() {
        let install = Path::new(r"C:\Users\example\AppData\Local\PhotoAI");
        assert!(paths_overlap(Path::new(r"c:\users\example"), install));
        assert!(paths_overlap(
            Path::new(r"C:\Users\example\AppData\Local\PhotoAI\data"),
            install
        ));
        assert!(path_is_prefix(Path::new(r"c:\users\example"), install));
        assert!(!path_is_prefix(
            Path::new(r"C:\Users\example\AppData\Local\PhotoAI\data"),
            install
        ));
        assert!(!paths_overlap(Path::new(r"D:\PhotoAI-Data"), install));
    }
}
