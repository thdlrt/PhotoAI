# 固定预算选片评测

本工具对照**明确填写的人工参考**比较评分结果，不运行模型，不读取或修改 RAW、预览、XMP、工程或人工调整。它是算法开发评测工具；测试通过不等于真实照片的选片效果已经改善。

## 生成和填写人工参考

在项目目录使用项目 Python：

```powershell
.venv/Scripts/python.exe scripts/evaluate_selection.py template --results <评分结果/results.json> --output <human-reference.json>
```

模板包含所有未删除、未移出的有效照片，不复制 AI 分数、星级或“未改动就等于人工认可”的结果。

逐张填写 `human_score`：0–5 整数，分数越高越值得保留；可以约定 0 为明显不可用、1–2 为较弱、3 为值得保留、4–5 为优秀。人工评分标准需在同批数据中一致，说明可写入 `notes`。`technical_failure` 填 true/false；无法判断时保留 null，不会自动视为“无问题”。

填写 `rater`（可用代号），将 `label_protocol` 改为 `independent`（未参考 AI 分数独立评审）或 `assisted`（看过 AI 建议）。工具只记录评审者声明，不能证明评审确实独立。不要修改 `photo_id`、`source_key` 和 `dataset_id`。

照片以相对路径和工程记录中的 `source_key` 匹配；`source_key` 是应用的文件版本标识，并非本工具重新计算的 RAW 内容哈希。评测不访问源照片。

调权重的数据与最终验收数据应分开，分别保存参考文件。每版算法在同一批照片上重新评分，保存各自不可变的 `results.json`。

## 比较版本

```powershell
.venv/Scripts/python.exe scripts/evaluate_selection.py evaluate --reference <human-reference.json> --results <before/results.json> <after/results.json> --output <comparison.json>
```

默认沿用人工参考中的保留比例，可显式传 `--retain-ratio` 做另一个预算实验。预算是精确十进制的 `ceil(有效照片数 × 比例)`；例如 100 张的 7% 是 7 张。两版都按综合分排序；同分沿用各评分文件原始顺序，与生产规则一致。人工调星不会偷偷替换被评估的 AI 排名。

每张都必须有人工分数。照片集合不一致、源文件标识变化、缺少标签、重复照片、非有限评分都会拒绝评测。不会只取“碰巧都有结果”的交集；旧结果缺少版本标识时应重新生成合适的评测结果。

输出包含输入文件 SHA-256，便于追溯。所有输出均拒绝覆盖已有文件，包括输入结果和人工参考。

## 指标含义

| 字段 | 含义 |
| --- | --- |
| `selected_human_mean` | AI 预算内照片的人工分均值，越高越好 |
| `ideal_human_mean` | 同一预算下，按人工分排序可达到的最高均值 |
| `human_mean_regret` | 上述两者之差，越小越好；0 表示预算内人工总分已达最优 |
| `ndcg_at_budget` | 预算内顺序质量；增益为 `2^人工分−1`、位置折扣为 `log2(名次+1)`，除以理想顺序值 |
| `preferred_precision` | 入选照片中人工分 ≥3 的比例 |
| `preferred_recall` | 所有人工分 ≥3 的照片中被选中的比例；严格预算不足时不可能全部召回 |
| `technical_failure_rate` | 已填写技术判断的入选照片中，明显问题照片的比例 |
| `technical_label_coverage` | 入选照片技术判断的标注覆盖率；不足 100% 时不能直接比较问题率 |

全部人工分为 0 时，nDCG 和优选召回率返回 null；没有技术标签时问题率返回 null。不会用 0 或 1 假装有结论。

`missed_above_selection_floor` 列出未入选但人工分高于入选最低分的照片；`selected_technical_failures` 列出人工标为明显问题却入选的照片，方便回到并排审片逐例检查。多个版本的差异应结合这些具体照片判断，不只看汇总指标。

当前自动化验收使用合成评分验证公式、输入防错、精确预算与文件保护，没有据此宣称真实审美提升。
