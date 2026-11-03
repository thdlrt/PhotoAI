# 摄影后期工具箱 CLI

CLI 供 Codex、其他 agent、脚本和人工终端使用，与桌面界面共享同一服务及工程。可执行入口是 `photography-toolbox`，别名 `photo-toolbox`；独立安装包提供 `PhotoToolbox.CLI.exe`。源码环境也可用：

```powershell
& .\.venv\Scripts\python.exe -m landscape_culler.toolbox_cli --help
```

以下示例使用 `photo-toolbox`。用独立 EXE 时直接替换命令名；含空格、中文的路径保留引号。

## 连接与数据目录

优先打开桌面工具箱，再连接它生成的记录：

```powershell
photo-toolbox --connection-file "D:\摄影工具箱数据\state\cli-session.json" capabilities
photo-toolbox --data-dir "D:\摄影工具箱数据\state" review list
```

`--data-dir` 可以传 state 目录，也可以传已初始化的内容根目录，CLI 会查找已有服务。Windows 连接凭据使用当前用户 DPAPI 保护；其他平台要求连接记录权限为 `0600`。连接前校验服务进程和实例身份。CLI 不接收命令行明文密钥，不输出会话、控制或 API 令牌，不使用桌面的一次性 bootstrap token。

开发服务可直接指定回环 URL：

```powershell
photo-toolbox --url http://127.0.0.1:8765 review list
```

CLI 不连接远程主机。全局连接选项放在子命令之前。`--request-timeout` 控制单次请求超时，默认 30 秒。

没有打开桌面时，可以在独立终端前台运行服务：

```powershell
photo-toolbox serve --content-root "D:\摄影工具箱数据"
```

该命令持续运行，输出脱敏服务地址和连接记录位置；不会返回一个虚构的后台任务。另一个终端通过该记录连接。同一数据目录只能有一个服务，重复启动会拒绝。关闭此服务会停止它持有的任务，长任务期间保持该进程运行。

离线只读模式无需启动服务：

```powershell
photo-toolbox --offline --data-dir "D:\摄影工具箱数据\state" review list
photo-toolbox --offline --data-dir "D:\摄影工具箱数据\state" timelapse jobs
```

离线模式支持目录、记录、用户选择、草案历史和导出读取；不构造任务管理器，也不把磁盘中的运行任务改成“已中断”。离线写入或计算返回 `service_required`。要操作工程，请连接现有服务或显式运行 `serve`。

## 机器接口与错误

除 `--help` 和长驻 `serve` 外，一次调用输出一个 UTF-8 JSON 对象，日志进入 stderr：

```json
{"protocol":"photography-toolbox/1","ok":true,"command":"review.list","data":{"workspaces":[]},"meta":{"transport":"http"}}
```

失败同样返回 JSON，并使用非零退出码：

```json
{"protocol":"photography-toolbox/1","ok":false,"command":"review.decide","error":{"code":"conflict","message":"记录已在另一窗口更新。请重新读取后合并修改。","details":{"status":409}}}
```

| 退出码 | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 操作、连接或 HTTP 错误 |
| 2 | 参数或 JSON 输入无效 |
| 3 | 修订号冲突或输出文件已存在 |
| 4 | 有限等待超时 |
| 5 | 此操作需要运行中的服务 |
| 130 | CLI 等待被中断 |

`schema` 不需要连接服务，输出全部命令参数、请求 JSON Schema、状态和语义约束：

```powershell
photo-toolbox schema
photo-toolbox schema --command review.decide
photo-toolbox --pretty schema --command storyboard.import
```

`--input-json` 读取 UTF-8 JSON 文件，也接受 `-` 从 stdin 读取；可以读取 CLI 完整成功信封中的 `data`。JSON 字段不会经过 shell 拼接。`--output` 指定的导出文件必须尚不存在，CLI 不覆盖原文件。

## AI 初筛与人工复评

原照片选片主线也通过同一 CLI 操作：

```powershell
photo-toolbox --data-dir "D:\摄影工具箱数据\state" photo projects
photo-toolbox --data-dir "D:\摄影工具箱数据\state" photo create --input-root "D:\旅行 照片"
photo-toolbox --data-dir "D:\摄影工具箱数据\state" photo group PROJECT_ID --wait
photo-toolbox --data-dir "D:\摄影工具箱数据\state" photo runs
photo-toolbox --data-dir "D:\摄影工具箱数据\state" photo open-run RUN_ID
photo-toolbox --data-dir "D:\摄影工具箱数据\state" photo score RUN_ID --revision 0 --wait
photo-toolbox --data-dir "D:\摄影工具箱数据\state" photo rate RUN_ID --revision 0 --input-json "D:\剪辑计划\照片星级.json"
```

`photo rate` 输入如 `{"indexes":[0,2],"rating":5}`，照片星级仍为 `0–5`，和协作审片的 `0–3` 维度评分不同。显式 `rating:null` 恢复 AI 星级。`photo groups/reorder/exclude` 分别调整分组、组顺序和工程排除项，输入字段通过 `schema --command photo.groups` 等查询。删除源文件和工程分别要求显式 `--confirmation delete-source-files` 与 `--confirmation "删除工程"`，沿用原 API 的保护与回收行为。

```powershell
photo-toolbox --data-dir "D:\摄影工具箱数据\state" review create --input-root "D:\旅行 素材\巴黎" --name "巴黎复评"
photo-toolbox --data-dir "D:\摄影工具箱数据\state" review get WORKSPACE_ID
photo-toolbox --data-dir "D:\摄影工具箱数据\state" review suggest WORKSPACE_ID --revision 0 --mode organize
photo-toolbox --data-dir "D:\摄影工具箱数据\state" review suggest WORKSPACE_ID --revision 1 --mode vision --limit 12 --wait --wait-timeout 180
```

`organize` 按目录/文件自然顺序生成初步编排；`existing_ai` 复用已有照片选片分数；`vision` 使用已配置模型，视频依据有限抽帧建议。它们更新 `assistant`，不会修改 `user` 或替用户作决定。

每个素材的 `user.revision` 用于保存个人选择；工作区的 `revision` 用于批次建议与重新扫描。先读取当前版本，再提交：

```powershell
photo-toolbox --data-dir "D:\摄影工具箱数据\state" review decide WORKSPACE_ID ASSET_ID --revision 0 --status keep --quality 3 --beauty 2 --memory 3 --notes "家人出镜，必须保留"
photo-toolbox --data-dir "D:\摄影工具箱数据\state" review decide WORKSPACE_ID ASSET_ID --revision 1 --input-json "D:\剪辑计划\片段选择.json"
```

用户状态：`keep` 必用、`maybe` 备选、`reject` 不用、`unreviewed` 未审。评分使用 `0` 未评分和 `1–3`；未评分不等于拒绝。视频可保存多个原片区间：

```json
{"segments":[{"in":2.5,"out":8.2,"note":"开门后的表情"},{"in":15.0,"out":20.5,"note":"街道全景"}],"chapter":"初到巴黎","order":2}
```

外部 agent 也可导入建议：

```powershell
photo-toolbox --data-dir "D:\摄影工具箱数据\state" review import WORKSPACE_ID --revision 4 --input-json "D:\剪辑计划\ai建议.json"
```

文件格式为 `{"items":{"ASSET_ID":{"status":"maybe","reason":"...","source":"external-agent"}}}`。人工选择始终独立。`review rescan WORKSPACE_ID --revision N` 重新扫描目录并保留已有决定。`review proxy WORKSPACE_ID ASSET_ID --start 0 --duration 60 --wait` 可生成短视频播放代理，原片不变。

## 等待用户与读取增量

`review get` 返回完整的 `assistant`、`user` 和 `user_revision`。`user_revision` 是所有素材的 `user.revision` 总和，仅用于变化检测；AI 建议增加工作区 `revision`，不会增加这个标识。

```powershell
photo-toolbox --data-dir "D:\摄影工具箱数据\state" review get WORKSPACE_ID | Set-Content -Encoding utf8 "D:\剪辑计划\baseline.json"
photo-toolbox --data-dir "D:\摄影工具箱数据\state" review watch --workspace WORKSPACE_ID --after-user-revision 3 --timeout 60
photo-toolbox --data-dir "D:\摄影工具箱数据\state" review changes WORKSPACE_ID --baseline "D:\剪辑计划\baseline.json"
```

`watch --after-user-revision` 适合等待用户复评；`watch --after-revision` 或 `review wait WORKSPACE_ID --after-revision N` 观察任意工作区变化。`changes` 比较基线，仅返回新增或用户决定变化的素材；加 `--wait` 可有限等待这个增量。没有变化则超时退出码为 4，不会生成确认、拒绝或批准。

## AI 编排与剪辑草案

```powershell
photo-toolbox --data-dir "D:\摄影工具箱数据\state" storyboard context WORKSPACE_ID
photo-toolbox --data-dir "D:\摄影工具箱数据\state" storyboard create WORKSPACE_ID --workspace-revision 5 --mode model --title "巴黎一天" --brief "按抵达、街景、晚餐组织" --wait
photo-toolbox --data-dir "D:\摄影工具箱数据\state" storyboard import WORKSPACE_ID --workspace-revision 5 --input-json "D:\剪辑计划\编排草案.json"
photo-toolbox --data-dir "D:\摄影工具箱数据\state" storyboard get WORKSPACE_ID PROPOSAL_ID
```

`context` 提供素材和人工约束，外部 agent 据此生成结构化 `chapters/clips`。导入示例：

```json
{"title":"巴黎一天","evidence":"依据人工必用选择和素材备注编排","chapters":[{"title":"初到巴黎","summary":"车站至酒店","clips":[{"asset_id":"ASSET_ID","source_in":2.5,"source_out":8.2,"reason":"人工指定的出站片段"}]}]}
```

导入和模型结果均为 `draft`。人工审核草案使用 `storyboard update`，同时携带草案 `--revision` 和 `--workspace-revision`；JSON 可写 `{"status":"approved"}`，也可修改标题、章节、片段。旧工作区版本不能批准。`storyboard history` 查看版本，`get --revision N` 读取旧版，`export --format json|csv` 导出剪辑清单。批准草案不会修改素材的个人选择，也不会直接启动视频剪辑或发布。

`storyboard export --revision N` 可导出历史版本；输出保留 `is_latest/latest_revision/approval_valid`，历史曾经批准不代表当前版本已批准。导入完整导出 JSON 时只提取标题、简述、章节和依据，不继承 `approved` 状态。

## 延时摄影与选片输出

```powershell
photo-toolbox --data-dir "D:\摄影工具箱数据\state" timelapse scan --input-root "D:\延时\日落 序列" --wait
photo-toolbox --data-dir "D:\摄影工具箱数据\state" timelapse get SESSION_ID
photo-toolbox --data-dir "D:\摄影工具箱数据\state" timelapse process SESSION_ID --revision 0 --keyframe-mode even --keyframe-count 8 --transition --wait
photo-toolbox --data-dir "D:\摄影工具箱数据\state" timelapse render SESSION_ID --revision 1 --fps 25 --width 3840 --height 2160 --crf 18 --wait --wait-timeout 600
```

完整参数可用 `--input-json`，字段见 `schema`。处理与视频合成成功后 `session.revision` 增加；旧版本写入会被拒绝。RAW 序列输出独立 XMP 后交给 Lightroom 导出成片序列；直接视频合成接受受支持的成片格式。

```powershell
photo-toolbox --data-dir "D:\摄影工具箱数据\state" selection list
photo-toolbox --data-dir "D:\摄影工具箱数据\state" selection get RUN_ID
photo-toolbox --data-dir "D:\摄影工具箱数据\state" selection export RUN_ID --format csv --output "D:\交接\入选清单.csv"
photo-toolbox --data-dir "D:\摄影工具箱数据\state" selection copy RUN_ID --revision 3 --output-dir "D:\交接\新选片目录" --wait
photo-toolbox --data-dir "D:\摄影工具箱数据\state" selection xmp RUN_ID --revision 3 --wait
```

选择输出不应用旧构图或调色。复制保留相对子目录、原片与已有 XMP，输出目录必须尚不存在。XMP 只为支持的专有 RAW 新建星级旁车，已有 XMP 跳过；仍保留事务和回滚。

## 文件工具与明确执行

文件工具的预览与执行是不同命令。预览会返回文件范围、候选项和 `plan_id`，不会自动接着执行：

```powershell
photo-toolbox --data-dir "D:\摄影工具箱数据\state" tools raw-jpeg-preview --input-json "D:\剪辑计划\孤片扫描.json"
photo-toolbox --data-dir "D:\摄影工具箱数据\state" tools raw-jpeg-execute --plan-id PLAN_ID --wait
photo-toolbox --data-dir "D:\摄影工具箱数据\state" tools raw-jpeg-rollback --transaction-id TRANSACTION_ID --wait
photo-toolbox --data-dir "D:\摄影工具箱数据\state" tools xmp-cleanup-preview --input-json "D:\剪辑计划\XMP扫描.json"
photo-toolbox --data-dir "D:\摄影工具箱数据\state" tools xmp-cleanup-execute --plan-id PLAN_ID --wait
```

RAW/成片预览输入示例：`{"layout":"mixed","direction":"jpeg","mixed_path":"D:/Photos/Trip","recursive":false}`。XMP 清理预览输入：`{"root_path":"D:/Photos/Trip","recursive":false}`。**清除 XMP 是永久删除**；执行必须显式指定已经审阅的计划 ID，后端仍重新校验文件范围和指纹。

`tools transactions --kind raw-jpeg|xmp-cleanup|xmp` 查询对应事务。`tools rollback --transaction-id ID --confirmation "撤销 XMP"` 撤销本工具创建的星级旁车，不能恢复永久清理掉的 XMP。所有原 API 的 Lightroom 状态、路径、计划状态与文件校验继续生效。

## 统一任务操作

异步命令默认返回任务；加 `--wait` 等待完成。任务类型为 `review`、`proxy`、`storyboard`、`timelapse`、`copy`、`service`；前三种需要工作区 ID。

```powershell
photo-toolbox --data-dir "D:\摄影工具箱数据\state" job get storyboard JOB_ID --workspace-id WORKSPACE_ID
photo-toolbox --data-dir "D:\摄影工具箱数据\state" job wait storyboard JOB_ID --workspace-id WORKSPACE_ID --timeout 120
photo-toolbox --data-dir "D:\摄影工具箱数据\state" job cancel timelapse JOB_ID
```

`--wait-timeout`/`job wait --timeout` 超时默认只结束等待，原任务继续；明确加 `--cancel-on-timeout` 才请求取消。取消是请求，后续查询终态确认。Ctrl+C 结束 CLI 等待不代表后台任务已取消。
