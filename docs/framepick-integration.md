# 视频整理 · 帧选

首页入口启动随安装包提供的 FramePick 1.3.0，保留完整预览界面与 .framepick 项目格式。项目文件、素材路径及分类目标不与照片选片或交互审片项目混用。

项目设置支持修改显示名称、选择 16 帧（4×4）或 25 帧（5×5）。右上角“重新生成”提供“全部重新生成”和“只补充”；两者都扫描全部导入目录的最新状况，未变文件的整理决定保留。只补充复用有效缓存，全部重新生成重做当前全部视频。16 帧保留旧缓存路径，25 帧独立缓存；缺少帧数字段的旧项目默认 16，打开未修改项目不重写文件。

默认项目位于工具箱数据目录的 framepick/projects，配置和缓存位于 framepick/state。用户也可自行选择其他项目文件位置。启动时仅传递系统必需环境变量，不把工具箱服务认证或云端密钥传给子程序。WMI 创建独立 GUI 进程，关闭工具箱不结束尚未关闭的帧选窗口。

开发构建需要 .NET 8 SDK，完整安装器构建入口会自动编译该组件。源码开发版可运行：

    ./packaging/build-framepick.ps1 -Destination ./integrations/framepick/output/FramePick

组件测试：

    dotnet run --project integrations/framepick/tests/FramePick.Tests -c Release -- --large
    dotnet run --project integrations/framepick/tests/FramePick.Tests -c Release -- --ui

工具箱 API：GET /api/toolbox/framepick；POST /api/toolbox/framepick/launch。启动接口要求现有应用会话和写入令牌，不接受任意可执行文件参数。

安装器同时包含 .NET/WPF 的原始许可证，最终用户不需要另装运行环境。媒体工具复用 tools/ffmpeg，不重复打包。
