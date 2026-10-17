# 作者与贡献说明

## 1. 作者

| 项目 | 内容 |
|---|---|
| 项目名 | **maa-arknights-auto** —— MAA × 雷电模拟器 × 明日方舟 无人值守挂机 |
| 作者 | **leo** &lt;1193725859@qq.com&gt; |
| GitHub | [@LIYuhao-u](https://github.com/LIYuhao-u) · 仓库 <https://github.com/LIYuhao-u/maa-arknights-auto> |
| 起始时间 | **2026-10-12**（首版代码 00:25 落地） |
| 主要开发期 | **2026-10-12 ~ 2026-10-18**（共 7 天，分 5 个里程碑，见 `CHANGELOG.md`） |
| 发布整理 | **2026-10-18** 首次公开到 GitHub，打标签 `v1.0.0` |
| 许可证 | MIT（见 `LICENSE`） |

## 2. 谁做了什么

| 角色 | 承担者 | 具体内容 | 时间 |
|---|---|---|---|
| 需求、决策与验收 | **leo** | 定下目标（定时唤醒 → 拉起雷电 → MAA 点「Link Start」→ 记录结果）；在「MAA 官方 Python 接口」与「界面自动化」之间拍板前者；决定关卡**跟随 MAA 界面**而非脚本硬编码；定下每天 04:10 / 16:10 两个时间点；选定看板形态为**零依赖静态单文件**；选定 **A+** 方案（静态看板 + 桌面快捷方式，不常驻服务）；逐轮验收并指出问题（如"退出码 0 但没真跑"、"关卡判断依据过期"） | 2026-10-12 起持续 |
| 代码与文档实现 | **AI 编程代理**（DeepSeek Harness，模型 deepseek-flash），在 leo 的指令与验收下逐步产出 | `maa_runner.py`、`maa_config.py`、`dashboard/build_dashboard.py`、全部 PowerShell 脚本、`config.json` 结构与注释、`README.md` 全部章节 | 2026-10-12 ~ 2026-10-18 |

> **关于 AI 参与的说明（据实披露）**
>
> 本仓库的代码由 AI 编程代理生成，**所有产品决策、目标设定与结果验收由 leo 完成**。每一轮改动都经过"提出 → 实现 → 实测 → leo 验收/否决"的循环，过程中被否决并推翻的方案包括：界面自动化路线、脚本硬编码 YW-8 关卡、常驻本地 HTTP 服务看板（B 方案）。
>
> 缺陷修复记录（真实发生过的问题与修法）集中记在 `README.md` 第 14 节「变更与踩坑记录」，包括：退出码被解释器析构污染、`pythonw` 下 `sys.stdout is None`、`.ps1` 缺 UTF-8 BOM 导致中文乱码、单实例锁被 BOM 破坏后误判为残留锁等。

## 3. 时间线（谁在什么时候做了什么）

| 日期 | 里程碑 | 主要内容 |
|---|---|---|
| 2026-10-12 | v0.1 骨架 | `config.json` / `maa_config.py`（GUI 任务队列 → MaaCore 参数映射）/ `maa_runner.py`（流程编排与回调采集）/ 计划任务脚本组；当天完成首次干跑与首次真实运行 |
| 2026-10-13 | v0.2 正确性 | 关卡有效性校验（对接 MAA 官方活动接口）、轻量全链路验证套件、退出码与结论判定修正 |
| 2026-10-17 | v0.3 可视化 | `dashboard/`（零依赖静态看板：KPI / 图表 / 可排序表格 / 详情 / 健康面板）、每次运行后自动重建 |
| 2026-10-17 | v0.4 可靠性 | 单实例锁（防定时与手动并发）、A+ 桌面快捷方式、计划任务漂移检测、README 整合为单篇 |
| 2026-10-18 | v1.0 发布 | 脱敏（个人路径移出仓库）、示例配置、作者/时间线/变更日志归档、依 MIT 协议公开 |

## 4. 引用与致谢

- [MAA — MaaAssistantArknights](https://github.com/MaaAssistantArknights/MaaAssistantArknights)：本项目通过其官方 Python 接口（`asst.py` → `MaaCore`）驱动，**不包含也不修改 MAA 的任何代码**。
- [MAA 官方数据接口](https://api.maa.plus/MaaAssistantArknights/api/gui/StageActivityV2.json)：关卡/活动有效性校验的数据来源。
- 雷电模拟器（LDPlayer 9）：由 `ldconsole.exe` 命令行控制，本项目同样不修改其程序。
