# 摄影后期工具箱

本地摄影后期工作台：AI 照片选片、照片与视频交互审片、AI 剪辑草案、LRTLite 延时摄影、素材整理，以及 Codex / agent 统一 CLI。当前版本 `0.10.0-beta.2`。

## 工作流

| 工具 | 输入 | 输出 |
| --- | --- | --- |
| AI 照片选片 | 照片 / RAW 目录 | 相似分组、AI 评分、人工星级、JSON/CSV 清单、选中原片副本、星级 XMP |
| 交互审片 | 照片与视频目录 | 独立的 AI 建议与人工决定、章节顺序、三项评分、备注、多段源 in/out、JSON/CSV |
| 延时摄影 · LRTLite | 同机位图像序列 / 已有 XMP | 亮度曲线、关键帧插值、曝光过渡与去闪 XMP、H.264 MP4 |
| 视频整理 · 帧选 | 视频目录，逐次添加 | 16 帧预览、独立项目分类、移动整理与回收站 |
| RAW / 成片管理 | 混合或分开的 RAW 与成片目录 | 配对预览、同盘回收与恢复记录 |
| XMP 写入记录 | 本工具创建的旁车文件 | 操作历史与可用回滚 |
| XMP 清理 | 指定目录 | 扫描后确认的永久删除 |

自动构图、基础自动调色、创意外观和风格库已从产品工作流移除。旧 `develop.json`、人工分组、星级和未知扩展字段保留；打开旧工程不迁移重写。新选择导出按白名单生成，不携带旧构图/调色配方，旧写操作和旧配方任务重试返回 410。

## 视频整理 · 帧选

工具箱首页点击「视频整理 · 帧选」打开随安装包提供的整理窗口。创建或打开 .framepick 项目后，每次导入一个文件夹，可累计添加；分类、目标目录和整理决定随项目保存。重复与重叠目录自动去重。

素材仍保留在原处，只有点击执行并确认后才移动或送入回收站。与选片和审片项目独立，原有星级、手动分组及旧字段保持不变。缓存、配置与默认项目目录归工具箱数据目录管理，复用工具箱随附 FFmpeg；最终用户无需安装 .NET SDK。

CLI 可用 tools framepick-status 查看组件状态，tools framepick-open 打开窗口。源码构建参见 [帧选集成说明](docs/framepick-integration.md)。

## 交互审片

1. 创建审片并扫描素材。原文件留在原处。
2. 可按目录编排、读取已有选片 AI 分数，或调用设置中已配置的视觉模型。
3. 人工选择为“必用 / 备选 / 不用 / 未审”；技术质量、画面美感和纪念意义各为 0–3 分，0 表示未评。键盘 `1 / 2 / 3 / 0` 改选择，左右方向键切素材。
4. 视频可记录多个原片时间区间。浏览器不能直接播放的编码可按需生成最多 60 秒 H.264 兼容预览，时间点映射回原片。
5. 改动在切素材、切页和导出前保存，服务器使用逐项修订号阻止覆盖并发修改。导出的选择以人工决定为准。

视觉评审每批默认最多 12 个素材，视频只采样 3 帧，不评声音、不代表连续看完。目录编排明确标记为规则建议。云端调用沿用已保存的服务与上传同意配置；本地使用受管 Ollama 与已安装模型。未配置时显示具体错误，不把规则结果冒充模型结果。

## 延时摄影

复用 `E:\code\rea\lrt-lite` 的 Python 核心到包内 `_vendor/lrtlite`，原工程保留。运行时不依赖该绝对路径。

扫描后可预览帧和亮度曲线；支持自动、等距、已有 XMP 和手动关键帧，手动关键帧可设置曝光 EV。选择关键帧插值、圣杯曝光补偿、去闪强度和平滑窗口后，处理结果保存到独立任务目录并可下载 XMP ZIP。

RAW 的正式显影由 Lightroom 等 RAW 软件完成，再导入 JPEG/TIFF 序列合成；不会将 RAW 内嵌缩略图冒充正式视频。成片序列可直接合成 1080p/4K/竖屏/方形 MP4，等比缩放补边。混合尺寸需明确勾选允许。处理、合成支持进度、取消和重启后的任务恢复查看。

## AI 编排与 Agent 协作

审片中的“AI 编排 / 剪辑草案”支持按人工选择生成初稿、调用已配置模型进行语义编排、导入外部 agent 提案。草案保留章节、素材顺序、照片停留时间、原片区间、理由和版本历史；用户必用/不用及已选区间受约束。批准只改变草案状态，之后素材选择发生变化会使批准失效。历史版本只读，可按具体修订导出。

统一 CLI 提供 `photo`、`review`、`storyboard`、`timelapse`、`selection`、`tools`、`job`，覆盖界面各模块。独立安装提供 `PhotoToolbox.CLI.exe` 和安装目录中的 `photography-toolbox.cmd`；源码入口为：

```powershell
& .\.venv\Scripts\python.exe -m landscape_culler.toolbox_cli schema
& .\.venv\Scripts\python.exe -m landscape_culler.toolbox_cli --url http://127.0.0.1:8765 review list
```

桌面服务通过当前账户保护的连接记录供CLI发现；CLI不打印密钥。`schema` 是机器可读能力说明，写入使用修订号，任务有ID、有限等待和取消；`review watch --after-user-revision` 可让Codex等待用户确认，AI建议更新不会误触发人工完成。离线模式只读，不启动第二个任务管理器。详见 [完整 CLI 用法](docs/photography-toolbox-cli.md)。
## 文件与旧项目

- 新选择导出可以下载 JSON/CSV 或后台复制原片。复制必须指定一个尚不存在的新目录，保留相对子目录和 XMP，逐文件校验，不移动或覆盖原片。
- 星级 XMP 仅为尚无旁车的专有 RAW 创建；已有 XMP 跳过。写入前需关闭 Lightroom，仍有事务记录与回滚。
- 选片新安装配置仅要求 DINOv2、Q-ReAlign，以及本地视觉模式下的 Qwen。旧构图/风格模型不再必装；已下载文件不自动删除。
- RAW/成片整理使用同盘 `.photo-ai-trash`；工程删除也进入应用回收目录。
- **“清除文件夹 XMP”沿用原工具的永久删除行为。** 执行前必须先预览并确认范围。
- 旧版功能说明保存在 [历史说明](docs/legacy-photoai-readme.md)，其中退休功能不代表当前版本仍提供。

## 开发与本地构建

Windows x64、Python 3.12、Node.js、Rust MSVC 与 Visual Studio C++ Build Tools。使用现有 `.venv` 或 `uv sync --extra dev` 后启动：

```powershell
.\scripts\env.ps1
uv run landscape-culler-web
```

开发服务仅绑定本机。桌面应用沿用 `PhotoAI.exe`、产品 ID 与原数据目录兼容边界。

```powershell
.\packaging\build-windows.ps1 -WithMediaTools -SkipNpmInstall
```

`-WithMediaTools` 包含已校验的便携 FFmpeg/FFprobe。完整工具未压缩约 402 MiB，二进制缓存不纳入 Git；来源、SHA-256、许可证与构建说明见 [媒体工具说明](licenses/ffmpeg/INTEGRATION.md)。不带此参数的基础构建需自行配置可用 FFmpeg/FFprobe 才能合成或生成视频预览。两种构建都不包含 AI 权重。

构建门禁验证打包 Service、三项工作流、旧项目只读兼容和真实小样视频，不再要求已退休的构图功能。
