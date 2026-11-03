# 摄影后期工具箱升级验收记录

日期：2026-10-25。工程：`E:\code\landscape-ai-culler`。

## 本次交付

- 产品改为摄影后期工具箱 0.10.0-beta.1，保留原产品身份和旧数据兼容边界。
- 删除主流程的自动构图、自动调色与风格库界面；选片直接进入选择清单、原片复制与星级 XMP。
- 旧构图/调色写接口和旧配方任务重试明确拒绝，历史读取不重写文件。
- 引入包内 LRTLite 核心，新增序列工作区、手动/自动关键帧、曝光插值、去闪、XMP ZIP、FFmpeg MP4。
- 新增照片/视频交互审片，建议和人工决定独立保存、逐项并发修订、章节/顺序/评分/备注/多段源区间、JSON/CSV。
- 视频兼容预览按需转码最多60秒；源时间偏移明确保留。
- 新安装配置移除四个构图/风格专用必需模型；已有模型未删除。

## 自动验收

聚焦回归命令覆盖 `test_photo_toolbox_integration.py`、`test_collaborative_review.py`、`test_timelapse_toolbox.py`、`test_selection_export.py`、`test_group_order.py`、资源与安装worker、XMP、打包、NSIS迁移。结果：第二阶段完整相关回归 207 passed。非阻断告警来自 Starlette 测试客户端和 Pillow 的既有弃用接口。

Rust 桌面测试：22 passed。新旧品牌 NSIS 所有权校验：旧品牌/新品牌通过，无关名称/错误marker拒绝。

打包 Service 隔离 PATH 验收：三工具入口、退休操作410、旧记录只读、审片保存/重读/预览、LRT XMP、便携 FFmpeg/FFprobe 小样合成/完整解码通过。最终安装器信息见本次 `output/toolbox-upgrade-20261025` 产物及构建回执。

## 实际浏览器验收（Edge / Codex 内置浏览器，合成素材）

- 创建18张照片的审片工程，保存“必用”和备注。
- 未点保存直接切到设置，再以深链接重开，备注与选择恢复。
- 目录编排不会覆盖人工决定，规则来源明确显示；浏览器无JS错误。
- 原片复制6张，全部完成，界面显示SHA-256校验通过；输出独立新目录。
- 延时扫描18帧，生成XMP和1920×1080、25fps、0.72秒MP4。
- 手动关键帧首帧0 EV、末帧0.5 EV：实际请求0.5，读取结果首帧0、中间0.24、末帧0.5；处理参数可恢复。
- 三秒视频从原片0.5秒开始生成兼容预览，添加区间回映射为原片秒数，并保存成功。
- 已修复切页丢输入、后台任务抢导航、深链接丢ID、旧复制任务串工程、EV输入未提交和预览图溢出。

合成数据均在 `output/toolbox-upgrade-20261025/qa`；没有上传用户照片或调用付费云模型；第二阶段另外用合成素材实际调用本地 Qwen3-VL。浏览器自动化标签已释放。

## 验证边界

- 已验证真实本地 Qwen3-VL 8B 的一张合成照片视觉建议和文字驱动的两章剪辑草案，以及调用协议、可控模型响应和本地媒体链；未以用户真实照片/视频衡量AI审美质量，也未验证真实云服务质量。
- RAW正式显影仍交给Lightroom等RAW软件；本次未做Lightroom实体导入/显影验收。
- 视频视觉建议是三帧采样，不是连续全片和音轨评审。
- 本地安装候选未覆盖当前安装，未发布。安装版启动会沿用既有数据目录配置，不声称具有独立便携模式。
- 原 `E:\code\rea` 工程、原片与旧项目保留。改动前主界面和README备份在本次output的baseline目录；此前dirty工作未回滚或提交。

## 后续建议

优先考虑批量成片导出（尺寸/格式/水印预设）、联系表，以及跨工具的任务队列。是否加入取决于实际摄影流程；本轮没有把它们做成占位按钮。

## 第二阶段：AI 编排与统一 CLI

用户补充要求后继续扩展，没有把阶段1候选当作最终完成。

- 规则初稿、真实配置模型语义编排、外部agent提案导入；草案可编辑、审核、退回、查看不可覆盖历史，批准受最新修订和素材选择版本约束。
- 统一 CLI 覆盖 photo/review/storyboard/timelapse/selection/tools/job，JSON schema/退出码/原子修订/任务等待取消/离线只读。
- CLI与界面共享服务：真实跨进程测试确认AI更新不唤醒human watch，人工决定唤醒；第二服务会被数据目录锁拒绝。
- 同账户DPAPI保护连接记录，先无凭据校验服务PID与实例；CLI握手、stdout/stderr不泄露令牌。安装提供独立exe，共用Service运行库。
- 207项相关Python回归通过；桌面Rust22项通过，真实NSIS旧品牌注册表路径迁移、CLI独立运行与资源发现另由打包门禁验证。
- 前端延迟响应行为测试通过，覆盖读历史、保存、批准、关闭/重开、模型完成与人工编辑之间的竞态。
- 本地真实模型为 qwen3-vl:8b-instruct。视觉job=448ee0e9776b43e98d92e773c7840146；编排job=c5c89e38e0a343e1b13ab247676fe578；生成两章草案proposal=71a50c500bd344c5b4b6895624371675。记录在output/toolbox-upgrade-20261025/real-local-ai-validation.json，均为合成测试素材。
- 真实浏览器保存新的人工备注，独立 CLI 的 `review watch --after-user-revision 4` 正常返回 `user_revision=5` 和新备注；AI 建议与人工选择的变化分别识别。
- 最后复验草案 `c1627a9d95fd42e891dd8495fb4fe640`：界面批准修订1，再切到历史修订0；CLI回读最新仍为修订1、approved、approval_valid=true，没有产生额外修订。历史界面只读，JSON/CSV链接带 `revision=0`。证据为 `output/toolbox-upgrade-20261025/storyboard-history-ui-readback.json`。
- Agent协作页面显示当前服务的可复制PowerShell命令和人工确认流程，无凭据输出。最终主页截图为 `output/toolbox-upgrade-20261025/toolbox-home-final.png`。

## 最终候选安装器

- 路径：`output/toolbox-upgrade-20261025/final/PhotoAI-Setup-x64.exe`。
- 版本：`0.10.0-beta.1`；大小：404,654,294 字节（约 385.9 MiB）。
- SHA-256：`1bd2bcac4f89a08565a2c6549086cdd82485fa609f0728cb1427a0f19e9a3aa6`。
- 体积门禁保留基础安装器350 MiB、总载荷700 MiB限制；仅对显式包含媒体工具的候选增加32 MiB媒体预算与实际CLI的11,975,135字节，上限16 MiB。测量、预算与核对回执保存在同目录。
- 阶段1候选保留在 `stage-1`；以上 `final` 候选包含后续AI草案与完整CLI，应使用最终候选。
- 后续提取与验收的TEMP/TMP显式设置在E盘项目临时目录；没有迁移既有工作工程或软件。
- 最终安装器提取验收通过，构建ID为 `75385c683485`：10个UI文件、wheel内77个源文件及83个冻结产品文件一致；真实Service/CLI、独立CLI serve、命令别名、媒体资源与桌面自检通过。见 `final/acceptance.json`。旧安装指针未变，没有执行覆盖安装。
