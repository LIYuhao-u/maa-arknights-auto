# 变更日志

本文件按**真实开发日期**记录。发布到 GitHub 时历史是按里程碑重建的（原始逐次保存的中间提交未保留），因此本日志比 `git log` 更细。

格式：`日期 · 版本 · 类型`。类型为 `新增 / 修复 / 变更 / 文档 / 安全`。

---

## 2026-10-25 · v1.1.0 · 关卡核对

- **新增** `analyze_stage_usage()`（`maa_runner.py`）：运行结束后把「配置的关卡」与「实际打到的关卡」逐任务对比。数据来源是 `StageDrops` 回调里的 `stageCode`；此前脚本只能校验关卡名是否有效，管不到 MAA 运行时到底进了哪一关。
- **新增** `report.json` 的 `stage_usage` 字段、`summary.txt` 的「关卡使用（配置 vs 实际）」章节、任务条目的 `battles`（每项作战场次，来自 `StartButton2` 子任务按 `taskid` 计数）。
- **新增** 三种可判定的异常：`mismatch`（配置 A 实际打 B）、`no-battle`（配置了关卡却一场没打成）、`unknown-stage`（打了但没识别到掉落结算，无法核对）；空关卡任务输出提示，写明它实际打的是哪一关。
- **新增** 构建期提示：某个作战任务的关卡为空时，运行前就说明「MAA 会打上一次作战，无法确定是哪一关」。
- **修复** 起因是一次真实事故：10-24 两次运行配置 `CA-5`，实际打了 7 场 / 6 场 `AD-8`（`剩余理智` 关卡为空 → MAA 走「前往上一次作战」），而报告当时只显示任务链状态，完全看不出打错了。详见 README 第 14 节。
- **测试** 用 3 次历史运行（10-24 ×2、10-25 ×1）的 `callbacks.jsonl` 回放给 `RunRecorder`：作战总数、逐任务分配、关卡核对结果与历史 `report.json` 完全一致；另有 7 组合成用例覆盖全部状态；`run_now.ps1 -DryRun` 通过。
- **文档** README 第 6/8/13/14 节同步更新。

---

## 2026-10-18 · v1.0.0 · 发布

- **变更** 个人路径与主机信息从仓库移出：新增 `config.example.json`，`config.json` 改为本机私有（`.gitignore` 忽略），克隆后按文档复制并改路径。
- **新增** `AUTHORS.md`（作者、谁做了什么、时间线、AI 参与披露）、`CHANGELOG.md`、`LICENSE`（MIT）、`.gitattributes`（PS1 固定 CRLF + BOM 语义）。
- **新增** 每个源文件头部的「作者 / 创建时间 / 最近修改」元信息。
- **文档** README 增补「作者与时间线」「仓库结构与快速开始」，并把状态快照标注为本机某一时刻的实测值。

## 2026-10-17 · v0.4.0 · 可靠性

- **新增** 单实例锁 `logs\.run.lock`（JSON：pid / 开始时间 / 模式 / 主机），`O_CREAT|O_EXCL` 原子创建；用 `OpenProcess + WaitForSingleObject` 判定持有者存活，避免 pid 复用误判；残留锁在持有进程已死或超过 `task_timeout+30` 分钟时自动接管；被挡下的触发**不写报告、不进统计**，只记 `logs\lock-conflicts.log`，退出码 `12`。
- **新增** `make_shortcuts.ps1`：A+ 方案三个桌面入口（立即运行 / 打开看板 / 定时状态），统一带 `-NoProfile -ExecutionPolicy Bypass`。
- **新增** `--ignore-lock` 应急开关。
- **修复** 锁文件由 PowerShell 写出时带 UTF-8 BOM，`json.loads` 解析失败 → 活锁被当残留锁抢占。改为按 `utf-8-sig` 读取，损坏但 mtime 在 10 分钟内的锁保守视为"占用中"。
- **修复** 看板内联柱状图不显示的样式问题；`GBK` 控制台因 `✔` 字符崩溃。
- **变更** 定时时间点由 04:10 / 12:10 / 20:10 收敛为 **04:10 / 16:10**；新增计划任务漂移检测（触发器与 `config.json` 不一致时明确提示需重跑 `register_schedule.ps1`）。
- **文档** README 合并为单篇（0~16 节），新增速查卡与卸载路径。

## 2026-10-17 · v0.3.0 · 可视化

- **新增** `dashboard/build_dashboard.py`（纯标准库、离线、单文件 HTML）：KPI 概览、按日结果堆叠柱图、耗时折线、任务链耗时条形（均为 Python 生成的 inline SVG）、可排序/筛选/搜索的运行记录表、单次运行详情、健康面板（计划任务状态、下次运行、配置摘要、配置漂移、错误归因）。
- **新增** `dashboard.ps1`（`-Open` / `-NoBuild` / `-IncludeDryRun`）与「每次运行结束自动重建」。
- **新增** `#run=<id>` 深链、页脚并发冲突统计、`--self-check` 自检模式。
- **变更** 默认统计口径排除 `dry-run`。

## 2026-10-13 · v0.2.0 · 正确性

- **新增** 关卡有效性校验：`run.verify_stage` 在运行前用 MAA 官方活动接口校验关卡是否仍有效，失效时按 `on_invalid_stage`（`skip` / `abort` / `allow`）处理，**绝不静默改打卡别的关卡**。
- **新增** 活动关卡导航数据自动刷新（`cache/resource/tasks.json`，TTL 6 小时），失败则沿用本地旧数据。
- **新增** 轻量全链路验证套件（`tests/make_verify_config.py` → 只跑 3 个低消耗任务）。
- **修复** 运行成功却返回退出码 `1`：MaaCore 在解释器析构阶段卸载异常覆盖了返回值，改为记录结论后用 `os._exit(code)` 退出。
- **修复** `SubTaskError CreditShoppingTask`（"信用不足"）等可恢复提示被当成致命错误，导致整次运行判 `failed`；改为按任务链状态判定结论，错误与提示分开记录。
- **修复** `pythonw.exe` 下 `sys.stdout is None` 导致的崩溃（日志层与看板生成器都加了保护）。
- **修复** `Runner` 中 `self.adb` 属性遮蔽同名方法，改为 `self.adb_path`。

## 2026-10-12 · v0.1.0 · 骨架

- **新增** `config.json`（`maa` / `emulator` / `run` / `tasks` / `report` / `schedule` / `dashboard` 七段，路径支持相对本文件）。
- **新增** `maa_config.py`：配置深度合并（忽略 `_` 开头的注释键）、**MAA GUI 任务队列 → MaaCore `AsstAppendTask` 参数**映射（含公招 `select/confirm` 按星级、基建换班、理智作战、信用收支等），未消费的 GUI 字段写入 `TaskSpec.unmapped` 并在报告中提示，杜绝配置被静默忽略。
- **新增** `maa_runner.py`：自检 → 启动雷电（含启动重试）→ 连接 `emulator-5554` → 执行任务链 → 全程收集回调（任务成败、掉落、公招、作战次数、理智药/碎石消耗、错误）→ 生成 `report.json` / `summary.txt` → 退出模拟器 → 清理残留进程。
- **新增** `_common.ps1` / `run_now.ps1` / `register_schedule.ps1` / `unregister_schedule.ps1` / `schedule_status.ps1`（计划任务：`WakeToRun` + `StartWhenAvailable` + `MultipleInstances=IgnoreNew`，`pythonw.exe` 静默运行）。
- **新增** 运行目录与索引 `logs\runs.jsonl`、`report.keep_days` 保留策略。
- **修复** 所有 `.ps1` 补 UTF-8 BOM（PowerShell 5.1 否则中文乱码），并逐文件用 `[Parser]::ParseFile` 验证可解析。
- **修复** `config.json` 中误用 `//` 行注释导致解析失败（退出码 4），改用 `_comment_*` 键。
