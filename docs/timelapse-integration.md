# 延时摄影模块

LRT Lite 0.2 数值核心随摄影后期工具箱一起分发，不需要启动原 Tk 界面，
也不依赖原 `E:/code/rea` 工作目录。导入来源见包内 `_vendor/lrtlite/ORIGIN.md`。

## 使用流程

1. 扫描单个序列文件夹，按文件名自然排序，读取曝光、原有 XMP、帧尺寸。
   RAW+JPEG 同名配对需要选一种格式；避免把同一时刻计算为两帧。
2. 选择自动、等距、已有或手选关键帧，按需启用曝光过渡、圣杯校正、去闪。
   曝光过渡只插值用户指定的关键帧曝光；不自动构图或推断色彩风格。
3. 导出新的 XMP ZIP。原始照片及原有 sidecar 始终保留，源 XMP 在扫描后变化
   会拒绝执行并要求重新扫描，以免忽略 Lightroom 的新调整。
4. 将需要的 XMP 应用于 Lightroom 工作副本并导出 JPEG/TIFF 后扫描成片序列，
   设置帧率、画面大小、质量，合成 H.264 MP4。全程保持比例并补黑边。
   混合尺寸默认阻止合成，必须明确选择允许混合尺寸。

对于已经显影的序列，也可以直接合成。`apply_corrections` 默认关闭；打开时，
仅应用本次 LRT Lite 增加的曝光补偿，采用 sRGB 线性化后曝光换算。该选项不渲染
Lightroom 色彩、局部蒙版或其他 XMP 设置；已在 Lightroom 应用补偿的导出序列不要重复打开。
RAW 内嵌图仅供预览与亮度分析，不作为正式视频帧。

## 接口与保存

工厂：`create_timelapse_router(data_dir, mutate_token)`；前缀 `/api/toolbox/timelapse`。
所有 POST 依赖主服务的 mutation token 检查。

| 路由 | 用途 |
| --- | --- |
| GET `/capabilities` | Pillow、FFmpeg、FFprobe、ExifTool 能力与路径 |
| POST `/scan` | `{input_root, extensions?}`，异步扫描任务 |
| GET `/sessions`、`/sessions/{id}` | 历史序列、帧列表、曝光曲线 |
| POST `/sessions/{id}/process` | 关键帧、曝光过渡、圣杯、去闪、XMP 导出 |
| POST `/sessions/{id}/render` | MP4 合成任务 |
| GET `/sessions/{id}/frames/{index}` | 帧预览，index 从 0 开始 |
| GET `/jobs`、`/jobs/{id}` | 持久任务历史与实时进度 |
| POST `/jobs/{id}/cancel` | 取消当前任务或队列任务 |
| GET `/jobs/{id}/video`、`/jobs/{id}/xmp` | 完成任务的 MP4 / XMP ZIP |

输出：`data_dir/toolbox/timelapse/sessions/<id>/outputs/<job_id>/`。
每次生成独立目录；重复处理从扫描时的原始 XMP 开始，不叠加旧补偿。
任务及序列重启后可见；重启时残留的 running/queued 状态改成 interrupted，需重新执行。
主服务关闭时调用 `router.timelapse_manager.close()`，仅取消本模块任务和编码进程。

工具发现依次使用 `PHOTO_TOOLBOX_FFMPEG/FFPROBE/EXIFTOOL`、
`PHOTO_AI_FFMPEG/FFPROBE/EXIFTOOL`、已打包 tools、项目 .runtime/tools、系统 PATH。
缺少 FFmpeg/FFprobe 时禁用合成能力；缺少 ExifTool 时普通图片仍可用 Pillow 读取，
RAW 曝光/预览能力可能受限。

本地候选已复用现有 Gyan 8.0.1 静态版至被 Git 忽略的
`packaging/vendor/ffmpeg-8.0.1/` 和 `desktop/src-tauri/resources/tools/ffmpeg/`。
正式构建脚本通过显式 `-WithMediaTools` 包含它，也可传 `-MediaToolsSource`。
两可执行文件合计约 402.44 MiB（安装包压缩前），没有加入 Git 文件跟踪。
许可、来源和二进制哈希见 `licenses/ffmpeg/INTEGRATION.md`；本次仅本地候选，未发布。
`packaging/verify-media-tools.py` 在仅保留 Windows System32 的 PATH 下验证版本、
12 帧合成、探测与完整解码，证明这两个副本不依赖 Conda DLL。

## 已验收

`tests/test_timelapse_toolbox.py` 验证自然排序、原片及原 XMP 哈希不变、未知 XMP 字段保留、
重复处理不叠加、XMP 变化拒绝、损坏 XMP 拒绝、RAW+JPEG 冲突、混尺寸预检、重启恢复、
队列取消、mutation token 与参数检查，以及 12 张合成测试图的真实 FFmpeg 合成、
FFprobe 帧数/尺寸检查和完整视频解码。未将真实摄影序列作为画面效果验收。
