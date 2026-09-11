# PhotoAI · 照片选片

[下载安装版](https://github.com/thdlrt/PhotoAI/releases) · [问题反馈](https://github.com/thdlrt/PhotoAI/issues) · [MIT 许可](LICENSE)

Windows 本地 AI 摄影工作流：先分组和选片，再按需构图、调色，最后统一导出。
当前为 **0.9.0-beta.1 测试版**，请先在照片副本上熟悉流程，并备份 Lightroom 目录和 XMP。

本地、非破坏性的 Windows 照片选片与 Lightroom 工作流。正式版是独立桌面程序，用户只会看到 `PhotoAI.exe`；内部服务、文件任务和 AI 任务均由程序按需启动，不需要预装 Python、uv、Conda、Ollama 或 CUDA Toolkit。

## 正式版使用

运行 `PhotoAI-Setup-x64.exe` 安装后可直接打开软件。首次启动默认使用程序安装目录下的 `data` 文件夹，不再强制弹出目录选择；用户可随时在“设置 → 数据目录”改到其他可写的本机 NTFS/ReFS 目录。修改位置不会自动搬移旧模型和工程，已配置的数据盘失联时仍必须重新连接原目录。

基础安装包不带模型。进入“设置 → 资源”，选择 NVIDIA 8GB 或 16GB 配置并点击一键安装。进度直接显示在当前运行组件或模型行下，包含当前动作、下载量、速度、ETA、续传量和已运行时间，不再占用一个独立大面板。程序只在当前数据目录内安装固定 Python 3.12、AI 依赖、便携 Ollama 和整套模型，逐项校验后才启用；安装中断可以取消和续传，半成品不会成为当前环境。Hugging Face 下载会并行探测大陆镜像与官方源，优先使用实测可达且更快的地址，并在单文件失败时自动切换。仅 NVIDIA 显卡驱动是外部前提，不需要系统 CUDA Toolkit。

桌面壳与内部服务会同时隔离系统中已有的 Ollama 地址和程序路径；受管 AI 只连接本次任务分配的随机回环端口，不会复用系统服务或固定 `11435`。

正式版布局：

```text
PhotoAI.exe
├─ PhotoAI.Service.exe
├─ PhotoAI.CoreWorker.exe
└─ data（默认，可在设置中修改）
   ├─ state / projects / backups
   ├─ runtimes / models / tools
   ├─ styles
   └─ cache / downloads / temp / logs
```

注册表长期只保存数据目录指针和安装版本。模型、运行环境、预览、WebView2 缓存、下载与日志都进入该数据目录；照片仍保留在用户原来的任意本地盘或 NAS 目录。

## 工作流

```text
创建工程 → 调整分组 → AI 评分 → 审片 → 构图 → 基础调色 → 创意外观 → 导出
```

- 新工程先分组，人工调整并确认后再评分；修改已确认分组会使旧评分及其后续方案失效。
- DINOv2 负责相似片分组；Q-ReAlign、Qwen3-VL 和技术质量共同完成组内与跨组评分。
- 构图、基础调色和创意外观都可跳过；真正写 XMP 或导出 JPEG 只发生在最终导出步骤。
- 智能构图、评分、风格推荐/预览和 LUT 成片渲染全部在受管 AI Worker 中运行，轻量桌面服务不会加载 Torch、OpenCV 或 OpenColorIO。
- 创意外观支持全局统一或按组设置，真实预览与最终输出由 Lightroom 使用相同外观身份、顺序和强度渲染；缓存键包含照片、调整和外观哈希，重复请求不会再次计算。
- 个人偏好逻辑回归训练已从主流程移除，旧工程和旧记录不会被自动删除。

## Lightroom 集成

Lightroom Classic 是可选外部集成，不影响工程、设置和非 AI 工具箱的启动。设置页会自动检测兼容版本，也可手动选择 Lightroom 路径并一键配置插件。插件安装到当前用户的 Adobe Lightroom `Modules` 目录，配置只保存当前数据目录对应的 bridge 根；发布模板不携带开发机路径。

Lightroom 插件按自身 SDK 最低要求接受 Lightroom Classic `14.3+`，不再设置未来版本上限；15.3 是完整验收版本，其他满足最低要求的版本会标记为“未完整验证”，并在插件连接后按实际能力使用。未安装或未连接时，Lightroom 基础自动调色、真实滤镜预览和 Lightroom JPEG 导出会显示不可用，但 AI 模型安装与 Lightroom 完全独立。插件队列只传内容哈希和资源身份，不向 Lightroom 传软件数据目录的绝对描述符路径。

## 工具箱与安全

- RAW/成片管理按“相对子目录 + 完整文件名（不含扩展名）”配对，支持 JPEG、HEIC、HEIF、HIF 与常见 RAW。执行前会重新扫描并校验，照片与对应 XMP 同进同退。
- RAW/成片整理的文件移除进入照片所在盘的 `.photo-ai-trash` 可恢复目录；不会跨盘复制大文件。
- **工具箱的“清除 XMP”是永久删除，不能撤销。** 执行前先预览并校验，只处理确认范围内的 `.xmp`，不处理普通 XML、RAW 或成片；该功能不需要 AI 模型。
- 选片和处理不会直接修改专有 RAW。最终 Lightroom 操作前后校验 RAW，XMP 使用旁车备份、原子写入和可回滚事务。
- 删除工程只删除本软件数据目录内的工程记录，不删除照片、RAW 或既有 XMP。
- 卸载默认保留数据目录；选择同时删除时仍需确认精确路径与体积，并通过产品 marker/所有权校验。

## 设置迁移

`.photoai-settings` 只包含 UI/工作流默认值、8GB/16GB 偏好、风格启停记录和导出默认参数。它不包含工程、照片、XMP、滤镜文件、模型、运行环境、硬件信息、令牌、Lightroom 路径或旧电脑绝对路径。新电脑导入后会重新检测硬件与 Lightroom，并由用户决定是否安装模型。

## 构建发布版

从源码开发需要 Windows x64、Python 3.12、uv，以及构建桌面壳所需的 Node.js、Rust MSVC 和 Visual Studio C++ Build Tools。最终用户不需要这些开发工具。

```powershell
git clone https://github.com/thdlrt/PhotoAI.git
cd PhotoAI
. .\scripts\env.ps1
uv sync --extra dev
uv run landscape-culler-web
```

这会启动开发用网页。仅安装基础依赖时可使用设置和文件工具；AI 功能还需要完整模型配置及受管计算环境。不要将开发服务暴露到公网。

在经过审核的 Windows 10/11 x64 构建机上运行：

```powershell
.\packaging\build-windows.ps1
```

日常修改后的增量验证可使用 `-Quick`：它复用 PyInstaller 与 Tauri 构建缓存、跳过重复 npm 安装，但仍执行资源门禁、自检、安装器体积和 SHA-256 检查。正式发布前再运行一次不带 `-Quick` 的完整构建。

该入口会构建两个轻量 PyInstaller `onedir` sidecar、AI Worker wheel、Tauri 桌面程序和当前用户范围 NSIS 安装器，并执行版本、协议、依赖边界、资源哈希、自检、安装包体积和模型排除门禁。输出位于：

```text
desktop\src-tauri\target\release\bundle\nsis\PhotoAI-Setup-x64.exe
```

详细发布说明及构建资源准备见 [packaging/README.md](packaging/README.md)。

## 开发兼容入口

仓库仍保留固定端口浏览器入口和命令行入口，仅用于本机开发与旧工作流兼容；它们不是正式安装版的启动链。正式版始终使用 Tauri 窗口、随机回环端口、一次性令牌和当前配置的数据目录。

## 开源范围与第三方资源

项目自有代码采用 MIT 许可。第三方组件、模型、用户导入的滤镜和 Adobe 软件不适用本项目的 MIT 授权，详见 [第三方声明](THIRD_PARTY_NOTICES.md)。

仓库不包含照片、工程记录、模型权重、Adobe 预设副本、本机插件配置或凭据。Release 提供不含模型的安装包；模型在安装后下载。Lightroom 需要用户自行安装并取得使用授权。

## 反馈

提交 Issue 时请提供操作步骤、软件版本、错误文本、Windows 版本及显卡型号。请勿公开包含照片目录、个人照片、访问令牌或 Lightroom 目录文件的完整数据包。测试覆盖不等于所有机型已验证；目前硬件实测以 RTX 5080 16GB 为主。
