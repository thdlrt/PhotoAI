# 未发布整包 · FramePick 1.3.0 · 2026-11-06

- 项目设置支持名称修改、16 帧（4×4）与 25 帧（5×5）。
- 重新生成提供全部重做与只补充，两者都会扫描全部导入目录的最新文件状况；未变文件的分类和删除决定保留。
- 旧项目默认16帧且打开不重写，16/25帧缓存隔离；修复极短视频 JPEG 编码兼容问题。
- 本机可更新帧选组件；现有 0.10.0-beta.2 Release 附件保持原版本。

# 0.10.0-beta.2 · 2026-11-03

- 摄影后期工具箱首页新增“视频整理 · 帧选”，Windows 安装包包含 FramePick 1.2.0 与独立 .NET/WPF 运行组件。
- 保留 .framepick 项目格式、逐个文件夹累计导入、项目分类与整理决定、16 帧预览、搜索排序、取消恢复和批量文件操作。
- 配置、缓存与默认项目位置归工具箱数据目录；复用已审核的随附 FFmpeg，不重复打包媒体工具。
- 新增统一 CLI tools framepick-status / framepick-open；启动要求工具箱现有写入令牌，不接受任意程序路径。
- 通过 Windows WMI 启动独立窗口，不依赖工具箱进程存活；只传递必需环境，不继承服务密钥。
- 本次发布包含此前完成的 0.10.0 工具箱工作流升级：交互审片、AI 剪辑草案、LRTLite 与统一 CLI，保留旧项目和人工调整。

# Changelog

## 0.9.0-beta.4

- Stage update installers outside the installed program directory, with an
  external helper working directory and TEMP/TMP, so nested data directories
  cannot make the updater block its own installation.
- Wait briefly for directory locks to clear and report a nonzero installer
  exit when the previous version cannot be staged. Existing data stays intact.
- Reuse the repaired beta.3 AI Worker for desktop-only updates by checking its
  visual-critique revision rather than requiring every desktop version to match.

## 0.9.0-beta.3

- Clarified confidence as 0–1 separately from 0–100 photo scores. Invalid model
  replies now receive the specific validation error on retry and leave bounded
  local diagnostics containing the failed reply and received value.
- Updated the visual-critique cache version and made older installed AI Workers
  request reconfiguration before running the old review implementation.
- Added confirmed source-file deletion for individual and batch photo selection,
  using a same-volume recycle directory and preserving manual review records.

## 0.9.0-beta.1 — first public release

- Windows desktop shell with a local service and isolated background workers.
- Group-first photo selection, editable groups, AI scoring and optional crop,
  basic-color and creative-look steps followed by unified export.
- NVIDIA 8GB/16GB resource profiles, post-install model download and offline
  resource import support; no model weights in the base installer.
- Optional Lightroom bridge for preview, processing and JPEG/XMP export.
- RAW/rendered-photo management and explicitly confirmed XMP cleanup.
- Fixed missing lazy-imported modules in packaged CoreWorker.
- Removed the accidental CLIP requirement from non-AI file tools.
- Added packaged XMP cleanup and file-preservation regression checks.

This is a beta, not a claim of full compatibility across every GPU, Lightroom
version or clean Windows installation. The 16GB setup was exercised on an RTX
5080; additional 8GB and clean-machine testing is welcome.
