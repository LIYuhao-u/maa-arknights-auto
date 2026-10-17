#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 作者: leo <1193725859@qq.com> · 创建 2026-10-17 · 最近修改 2026-10-17
# 项目: maa-arknights-auto（MIT License） · 见 README.md / AUTHORS.md

"""MAA 自动挂机 · 可视化看板生成器（纯标准库、离线、单文件 HTML）。

读 logs\\runs.jsonl 与各次运行的 report.json，生成 dashboard\\index.html：
  · KPI 概览、按日结果堆叠柱图、耗时折线、任务链平均耗时条形（全部由 Python 生成 inline SVG）
  · 可排序/筛选/搜索的运行记录表、点击查看单次详情（vanilla JS，无任何外部依赖）
  · 健康面板：计划任务状态、下次运行、配置摘要、配置漂移检测、错误归因

用法：
  python build_dashboard.py                    # 生成 dashboard\\index.html
  python build_dashboard.py --include-dry-run  # 图表也统计 dry-run 自检记录
  python build_dashboard.py --self-check       # 自检：构建到临时目录并校验结构
  python build_dashboard.py --no-task-query    # 不查询 Windows 计划任务
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import maa_config as mc  # noqa: E402

# --------------------------------------------------------------------------------------
# 常量：颜色 / 文案
# --------------------------------------------------------------------------------------

RESULT_META = OrderedDict(
    [
        ("success", ("成功", "#16a34a", "ok")),
        ("partial", ("部分完成", "#f59e0b", "warn")),
        ("failed", ("失败", "#dc2626", "bad")),
        ("aborted", ("被中断", "#9333ea", "bad")),
        ("timeout", ("超时", "#c2410c", "bad")),
        ("unexpected", ("异常", "#7f1d1d", "bad")),
        ("env-error", ("环境失败", "#64748b", "muted")),
        ("dry-run", ("干跑自检", "#0ea5e9", "info")),
    ]
)

TASK_STATUS = {
    "completed": ("完成", "#16a34a"),
    "error": ("失败", "#dc2626"),
    "stopped": ("已停止", "#f59e0b"),
    "running": ("未结束", "#0ea5e9"),
    "not-run": ("未执行", "#64748b"),
    "unknown": ("未知", "#94a3b8"),
}

TASK_RESULT_CODE = {
    0: "成功",
    2: "部分完成",
    3: "失败",
    4: "环境自检失败",
    5: "超时/判定卡死",
    9: "运行器崩溃",
    267009: "正在运行",
    267011: "尚未运行",
    267014: "被用户终止",
}

STAGE_KIND = {
    "activity": "活动关卡",
    "permanent": "常规关卡",
    "empty": "当前/上次关卡",
    "expired": "已过期",
    "unknown": "未知关卡",
    "unverified": "无法校验",
}

# 图表宽高（viewBox 坐标，页面用 width:100% 自适应）
CH_W, CH_H = 720, 230


# --------------------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------------------


def esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def say(message: str) -> None:
    """安全打印：pythonw 下 sys.stdout 为 None，GBK 控制台打印符号也会抛异常。"""
    stream = sys.stdout
    if stream is None:
        return
    try:
        print(message, flush=True)
    except (UnicodeEncodeError, OSError, ValueError):
        try:
            print(message.encode("gbk", "replace").decode("gbk"), flush=True)
        except (OSError, ValueError):
            pass


def fmt_duration(seconds: float | None) -> str:
    """秒 → 12m34s / 1h02m / 8s"""
    try:
        value = int(float(seconds or 0))
    except (TypeError, ValueError):
        return "-"
    if value <= 0:
        return "-"
    hours, rest = divmod(value, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def fmt_ts(raw: Any) -> str:
    """计划任务时间归一化：10/17/2026 16:10:00 与 2026-10-17T16:10:00 → 10-17 16:10。"""
    text = str(raw or "").strip().replace("T", " ")
    if not text:
        return "-"
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text[:19], fmt).strftime("%m-%d %H:%M")
        except ValueError:
            continue
    return text[:16]


def result_label(result: str) -> str:
    return RESULT_META.get(result, (result, "#94a3b8", "muted"))[0]


def result_color(result: str) -> str:
    return RESULT_META.get(result, (result, "#94a3b8", "muted"))[1]


def run_capture(cmd: list[str], timeout: float = 30) -> tuple[int, str]:
    """执行外部命令并捕获输出（用临时文件而不是管道，避免编码与受限环境问题）。"""
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".out")
    handle.close()
    path = Path(handle.name)
    try:
        with path.open("wb") as sink:
            proc = subprocess.run(
                cmd,
                stdout=sink,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                timeout=timeout,
            )
        return proc.returncode, path.read_bytes().decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -1, "命令超时"
    except OSError as exc:
        return -2, f"执行失败：{exc}"
    finally:
        try:
            path.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------------------
# 数据读取
# --------------------------------------------------------------------------------------


def read_runs(logs_dir: Path) -> tuple[list[dict], int]:
    """读 runs.jsonl，返回 (记录列表, 坏行数)。"""
    index_file = logs_dir / "runs.jsonl"
    if not index_file.exists():
        return [], 0
    rows: list[dict] = []
    bad = 0
    try:
        text = index_file.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return [], 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            bad += 1
            continue
        if isinstance(item, dict):
            rows.append(item)
        else:
            bad += 1
    return rows, bad


def extract_run(row: dict, logs_dir: Path) -> dict:
    """把一条索引记录 + 对应 report.json 压成看板用的紧凑结构。"""
    run_id = str(row.get("run_id") or "")
    record: dict[str, Any] = {
        "id": run_id,
        "mode": str(row.get("mode") or ""),
        "result": str(row.get("result") or "unknown"),
        "started": str(row.get("started_at") or ""),
        "finished": str(row.get("finished_at") or ""),
        "dur": float(row.get("duration_sec") or 0),
        "done": int(row.get("tasks_completed") or 0),
        "total": int(row.get("tasks_total") or 0),
        "battles": int(row.get("battles") or 0),
        "med": int(row.get("medicine_used") or 0),
        "stone": int(row.get("stone_used") or 0),
        "errs": int(row.get("errors") or 0),
        "notices": int(row.get("notices") or 0),
        "stage": "",
        "stage_kind": "",
        "tasks": [],
        "issues": [],
        "drops": {},
        "warnings": [],
        "has_report": False,
    }

    report_file = logs_dir / run_id / "report.json"
    try:
        report = json.loads(report_file.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return record
    if not isinstance(report, dict):
        return record
    record["has_report"] = True

    for check in report.get("stage_checks") or []:
        if isinstance(check, dict) and check.get("stage"):
            record["stage"] = str(check["stage"])
            record["stage_kind"] = str(check.get("kind") or "")
            break
    if not record["stage"]:
        for task in report.get("tasks") or []:
            if isinstance(task, dict) and task.get("core_type") == "Fight":
                stage = str((task.get("params") or {}).get("stage") or "")
                if stage:
                    record["stage"] = stage
                    break

    for task in report.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        record["tasks"].append(
            {
                "label": str(task.get("label") or task.get("core_type") or ""),
                "type": str(task.get("core_type") or ""),
                "status": str(task.get("status") or ""),
                "dur": float(task.get("duration_sec") or 0),
                "error": str(task.get("error") or ""),
            }
        )

    for issue in report.get("errors") or []:
        if isinstance(issue, dict):
            record["issues"].append(
                {"kind": str(issue.get("kind") or ""), "msg": str(issue.get("message") or ""), "fatal": True}
            )
    for issue in report.get("notices") or []:
        if isinstance(issue, dict):
            record["issues"].append(
                {"kind": str(issue.get("kind") or ""), "msg": str(issue.get("message") or ""), "fatal": False}
            )

    record["drops"] = report.get("drops") or {}
    record["warnings"] = [str(w) for w in (report.get("warnings") or [])]
    return record


def build_dataset(logs_dir: Path, max_runs: int) -> tuple[list[dict], int, int]:
    """返回 (按时间升序的运行记录, 坏行数, 总条数)。"""
    rows, bad = read_runs(logs_dir)
    total = len(rows)
    if max_runs > 0:
        rows = rows[-max_runs:]
    records = [extract_run(row, logs_dir) for row in rows]
    records.sort(key=lambda item: item["started"] or item["id"])
    return records, bad, total


# --------------------------------------------------------------------------------------
# 计划任务状态（Windows 事实）
# --------------------------------------------------------------------------------------


def read_conflict_stats(logs_dir: Path) -> dict:
    """统计 logs/lock-conflicts.log 的冲突条数（总/今日）。"""
    path = logs_dir / "lock-conflicts.log"
    stats = {"total": 0, "today": 0}
    if not path.exists():
        return stats
    try:
        lines = [l for l in path.read_text(encoding="utf-8-sig", errors="replace").splitlines() if l.strip()]
    except OSError:
        return stats
    today = datetime.now().strftime("%Y-%m-%d")
    stats["total"] = len(lines)
    stats["today"] = sum(1 for line in lines if today in line[:12])
    return stats


def query_task_state(task_name: str) -> dict | None:
    if not task_name or not re.fullmatch(r"[A-Za-z0-9_.\-]+", task_name):
        return None
    script = (
        "$ErrorActionPreference='Stop';"
        f"$t=Get-ScheduledTask -TaskName '{task_name}' -ErrorAction Stop;"
        f"$i=Get-ScheduledTaskInfo -TaskName '{task_name}';"
        "@{state=[string]$t.State;"
        "triggers=@($t.Triggers | ForEach-Object { ([datetime]$_.StartBoundary).ToString('HH:mm') });"
        "next=[string]$i.NextRunTime;last=[string]$i.LastRunTime;"
        "last_result=$i.LastTaskResult;missed=$i.NumberOfMissedRuns} | ConvertTo-Json -Compress"
    )
    code, out = run_capture(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script], timeout=25
    )
    if code != 0:
        return None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data
    return None


# --------------------------------------------------------------------------------------
# 聚合
# --------------------------------------------------------------------------------------


def aggregate(records: list[dict], charts_include_dry: bool) -> dict:
    scope = [r for r in records if charts_include_dry or r["result"] != "dry-run"]
    real = [r for r in scope if r["result"] != "dry-run"]

    # 按日（近 14 天，截止到今天与最近一次运行中较晚者）
    today = datetime.now().date()
    latest = today
    for record in real:
        try:
            day = datetime.strptime(record["started"][:10], "%Y-%m-%d").date()
        except ValueError:
            continue
        latest = max(latest, day)
    days = [latest - timedelta(days=13 - i) for i in range(14)]
    by_day = {d.isoformat(): {"success": 0, "partial": 0, "failed": 0, "env-error": 0, "other": 0} for d in days}
    for record in scope:
        bucket = by_day.get(record["started"][:10])
        if bucket is None:
            continue
        key = "other"
        if record["result"] == "success":
            key = "success"
        elif record["result"] == "partial":
            key = "partial"
        elif record["result"] in {"failed", "aborted", "timeout", "unexpected"}:
            key = "failed"
        elif record["result"] == "env-error":
            key = "env-error"
        bucket[key] += 1

    # 耗时序列（按时间序）
    durations = [(r["started"] or r["id"], r["dur"] / 60.0) for r in real if r["dur"] > 0]

    # 任务链平均耗时
    task_acc: dict[str, list[float]] = {}
    task_status: dict[str, Counter] = {}
    for record in real:
        for task in record["tasks"]:
            if not task["label"]:
                continue
            task_status.setdefault(task["label"], Counter())[task["status"]] += 1
            if task["dur"] > 0:
                task_acc.setdefault(task["label"], []).append(task["dur"])
    task_avg = [
        {
            "label": label,
            "avg": sum(values) / len(values),
            "n": len(values),
            "ok": task_status.get(label, Counter()).get("completed", 0),
            "bad": sum(
                count
                for status, count in task_status.get(label, Counter()).items()
                if status in {"error", "stopped"}
            ),
        }
        for label, values in task_acc.items()
    ]
    task_avg.sort(key=lambda item: item["avg"], reverse=True)

    # 错误/提示归因
    issue_counter: Counter = Counter()
    issue_fatal: dict[tuple[str, str], bool] = {}
    for record in real:
        for issue in record["issues"]:
            key = (issue["kind"], issue["msg"])
            issue_counter[key] += 1
            issue_fatal[key] = issue_fatal.get(key, False) or bool(issue["fatal"])
    issues_top = [
        {"kind": kind, "msg": msg, "n": count, "fatal": issue_fatal[(kind, msg)]}
        for (kind, msg), count in issue_counter.most_common(6)
    ]

    totals = {
        "runs": len(real),
        "success": sum(1 for r in real if r["result"] == "success"),
        "partial": sum(1 for r in real if r["result"] == "partial"),
        "bad": sum(1 for r in real if r["result"] in {"failed", "aborted", "timeout", "unexpected"}),
        "env_error": sum(1 for r in real if r["result"] == "env-error"),
        "battles": sum(r["battles"] for r in real),
        "medicine": sum(r["med"] for r in real),
        "stone": sum(r["stone"] for r in real),
        "duration": sum(r["dur"] for r in real),
    }
    totals["avg_duration"] = (totals["duration"] / totals["runs"]) if totals["runs"] else 0.0

    latest_run = real[-1] if real else (records[-1] if records else None)
    return {
        "scope": {"runs": len(scope), "real": len(real)},
        "by_day": [{"date": day, **counts} for day, counts in by_day.items()],
        "durations": durations,
        "task_avg": task_avg,
        "issues_top": issues_top,
        "totals": totals,
        "latest": latest_run,
        "dry_run_count": sum(1 for r in records if r["result"] == "dry-run"),
    }


# --------------------------------------------------------------------------------------
# 图表（inline SVG）
# --------------------------------------------------------------------------------------


def _svg_open(title: str) -> list[str]:
    return [
        f'<svg viewBox="0 0 {CH_W} {CH_H}" class="chart" role="img" aria-label="{esc(title)}">',
        '<rect x="0" y="0" width="720" height="230" fill="#ffffff"/>',
    ]


def _legend(items: list[tuple[str, str]]) -> list[str]:
    out = []
    x = CH_W - 12
    for label, color in reversed(items):
        width = 8 * len(label) + 34
        x -= width
        out.append(f'<rect x="{x}" y="6" width="10" height="10" rx="2" fill="{color}"/>')
        out.append(f'<text x="{x + 15}" y="15" font-size="11" fill="#475569">{esc(label)}</text>')
    return out


def svg_daily_bars(by_day: list[dict]) -> str:
    """按日结果堆叠柱图。"""
    parts = _svg_open("按日运行结果")
    parts += _legend([("成功", "#16a34a"), ("部分完成", "#f59e0b"), ("失败", "#dc2626"), ("环境失败", "#64748b")])
    left, right, top, bottom = 34, CH_W - 12, 34, CH_H - 30
    plot_h = bottom - top
    slots = max(1, len(by_day))
    totals = [d["success"] + d["partial"] + d["failed"] + d["env-error"] + d["other"] for d in by_day]
    peak = max(totals + [1])
    step = 1 if peak <= 4 else (2 if peak <= 10 else 5)
    for value in range(0, peak + 1, step):
        y = bottom - plot_h * value / peak
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#e2e8f0"/>')
        parts.append(f'<text x="{left - 6}" y="{y + 3.5:.1f}" font-size="10" fill="#94a3b8" text-anchor="end">{value}</text>')
    slot_w = (right - left) / slots
    bar_w = min(22.0, slot_w * 0.55)
    for index, day in enumerate(by_day):
        cx = left + slot_w * (index + 0.5)
        y_cursor = bottom
        for key, color in (("success", "#16a34a"), ("partial", "#f59e0b"), ("failed", "#dc2626"), ("env-error", "#64748b")):
            count = day[key]
            if count <= 0:
                continue
            height = plot_h * count / peak
            y_cursor -= height
            parts.append(
                f'<rect x="{cx - bar_w / 2:.1f}" y="{y_cursor:.1f}" width="{bar_w:.1f}" '
                f'height="{max(height, 1.5):.1f}" rx="2" fill="{color}"><title>{esc(day["date"])} {esc(key)} ×{count}</title></rect>'
            )
        label = day["date"][5:]
        parts.append(
            f'<text x="{cx:.1f}" y="{bottom + 14}" font-size="10" fill="#64748b" text-anchor="middle">{esc(label)}</text>'
        )
    parts.append(
        f'<text x="{left}" y="{CH_H - 4}" font-size="10" fill="#94a3b8">最近 14 天（按运行开始日期）</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


def svg_duration_line(series: list[tuple[str, float]]) -> str:
    """每次运行耗时折线（分钟）。"""
    parts = _svg_open("每次运行耗时")
    if not series:
        parts.append('<text x="360" y="120" font-size="13" fill="#94a3b8" text-anchor="middle">暂无耗时数据</text>')
        parts.append("</svg>")
        return "".join(parts)

    left, right, top, bottom = 40, CH_W - 14, 28, CH_H - 32
    plot_h, plot_w = bottom - top, right - left
    values = [value for _, value in series]
    peak = max(values + [1.0])
    top_value = peak * 1.15
    step = max(1, round(top_value / 4))
    for index in range(5):
        value = step * index
        if value > top_value:
            break
        y = bottom - plot_h * value / top_value
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#e2e8f0"/>')
        parts.append(
            f'<text x="{left - 6}" y="{y + 3.5:.1f}" font-size="10" fill="#94a3b8" text-anchor="end">{value}</text>'
        )
    mean = sum(values) / len(values)
    mean_y = bottom - plot_h * mean / top_value
    parts.append(
        f'<line x1="{left}" y1="{mean_y:.1f}" x2="{right}" y2="{mean_y:.1f}" stroke="#f59e0b" '
        f'stroke-dasharray="5 4" stroke-width="1.4"/>'
    )
    parts.append(
        f'<text x="{right - 2}" y="{mean_y - 5:.1f}" font-size="10" fill="#b45309" text-anchor="end">均值 {mean:.1f} min</text>'
    )

    count = len(series)
    points = []
    for index, (_, value) in enumerate(series):
        x = left + (plot_w * index / (count - 1) if count > 1 else plot_w / 2)
        y = bottom - plot_h * value / top_value
        points.append((x, y, value))
    if count > 1:
        polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in points)
        parts.append(f'<polyline points="{polyline}" fill="none" stroke="#0284c7" stroke-width="2"/>')
    for x, y, value in points:
        parts.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="#0284c7"><title>{value:.1f} min</title></circle>'
        )
    for index in (0, count // 2, count - 1):
        if 0 <= index < count:
            x, _, _ = points[index]
            label = (series[index][0] or "")[5:16]
            anchor = "start" if index == 0 else ("end" if index == count - 1 else "middle")
            parts.append(
                f'<text x="{x:.1f}" y="{bottom + 14}" font-size="10" fill="#64748b" text-anchor="{anchor}">{esc(label)}</text>'
            )
    parts.append(f'<text x="{left}" y="{CH_H - 4}" font-size="10" fill="#94a3b8">按时间顺序 · {count} 次运行</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_task_bars(task_avg: list[dict]) -> str:
    """任务链平均耗时横向条形。"""
    parts = _svg_open("任务链平均耗时")
    if not task_avg:
        parts.append('<text x="360" y="120" font-size="13" fill="#94a3b8" text-anchor="middle">暂无任务耗时数据</text>')
        parts.append("</svg>")
        return "".join(parts)

    label_w = 96
    left, right = label_w + 12, CH_W - 104
    top = 30
    row_h = min(24.0, (CH_H - top - 16) / max(1, len(task_avg)))
    peak = max(item["avg"] for item in task_avg) or 1.0
    for index, item in enumerate(task_avg):
        y = top + index * row_h
        value = item["avg"]
        width = (right - left) * value / peak
        ratio = item["ok"] / max(1, item["ok"] + item["bad"])
        color = "#16a34a" if item["bad"] == 0 else ("#f59e0b" if ratio >= 0.7 else "#dc2626")
        parts.append(
            f'<text x="{label_w}" y="{y + row_h * 0.62:.1f}" font-size="11" fill="#334155" text-anchor="end">{esc(item["label"])}</text>'
        )
        parts.append(
            f'<rect x="{left}" y="{y + row_h * 0.2:.1f}" width="{max(width, 2):.1f}" height="{row_h * 0.6:.1f}" '
            f'rx="3" fill="{color}" opacity="0.85"><title>{esc(item["label"])} 平均 {value / 60:.1f} min ×{item["n"]}</title></rect>'
        )
        parts.append(
            f'<text x="{left + max(width, 2) + 6:.1f}" y="{y + row_h * 0.62:.1f}" font-size="10" fill="#475569">'
            f'{value / 60:.1f}m · {item["ok"]}/{item["ok"] + item["bad"]}</text>'
        )
    parts.append(
        f'<text x="{left}" y="{CH_H - 4}" font-size="10" fill="#94a3b8">绿色=从未失败 · 黄色=偶有失败 · 红色=经常失败</text>'
    )
    parts.append("</svg>")
    return "".join(parts)


# --------------------------------------------------------------------------------------
# HTML 片段
# --------------------------------------------------------------------------------------

CSS = """
:root{--bg:#f6f8fb;--card:#ffffff;--line:#e2e8f0;--ink:#0f172a;--muted:#64748b;
--ok:#16a34a;--warn:#f59e0b;--bad:#dc2626;--info:#0ea5e9;--muted2:#94a3b8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.5 "Segoe UI","Microsoft YaHei",system-ui,-apple-system,sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:22px 20px 48px}
header.top{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between;margin-bottom:6px}
h1{font-size:21px;margin:0;font-weight:650;letter-spacing:.2px}
.sub{color:var(--muted);font-size:12px}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid transparent}
.pill.ok{background:#dcfce7;color:#15803d;border-color:#bbf7d0}
.pill.warn{background:#fef3c7;color:#b45309;border-color:#fde68a}
.pill.bad{background:#fee2e2;color:#b91c1c;border-color:#fecaca}
.pill.muted{background:#e2e8f0;color:#475569;border-color:#cbd5e1}
.pill.info{background:#e0f2fe;color:#0369a1;border-color:#bae6fd}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(172px,1fr));gap:12px;margin:16px 0 18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;
box-shadow:0 1px 2px rgba(15,23,42,.04)}
.card .k{color:var(--muted);font-size:12px;margin-bottom:6px}
.card .v{font-size:22px;font-weight:680;letter-spacing:.3px}
.card .h{color:var(--muted);font-size:11.5px;margin-top:4px;min-height:16px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:16px;
box-shadow:0 1px 2px rgba(15,23,42,.04)}
.panel h2{font-size:14.5px;margin:0 0 10px;font-weight:650;display:flex;justify-content:space-between;align-items:center}
.panel h2 .hint{font-size:11.5px;color:var(--muted);font-weight:400}
.chart{width:100%;height:auto;display:block}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:880px){.grid2{grid-template-columns:1fr}}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:10px;font-size:12.5px;color:var(--muted)}
.toolbar select,.toolbar input[type=text],.toolbar input[type=date]{
padding:5px 8px;border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--ink);font-size:12.5px}
.toolbar label{display:inline-flex;gap:5px;align-items:center;cursor:pointer}
table{width:100%;border-collapse:collapse;font-size:12.8px}
th,td{padding:7px 8px;text-align:left;border-bottom:1px solid #eef2f7;white-space:nowrap}
th{position:sticky;top:0;background:#f8fafc;color:#475569;font-weight:600;cursor:pointer;user-select:none;z-index:1}
th:hover{background:#eef2f7}
th .arw{color:#94a3b8;font-size:10px;margin-left:3px}
tbody tr{cursor:pointer}
tbody tr:hover{background:#f8fbff}
tbody tr.sel{background:#e8f2ff;box-shadow:inset 3px 0 0 #2563eb}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.scroll{max-height:430px;overflow:auto;border:1px solid var(--line);border-radius:10px}
.detail{margin-top:14px;border:1px solid #c7d2fe;background:#f5f8ff;border-radius:12px;padding:14px 16px}
.detail h3{margin:0 0 8px;font-size:14px;font-weight:650}
.detail .meta{color:var(--muted);font-size:12px;margin-bottom:10px}
.taskbar{display:grid;grid-template-columns:120px 1fr 76px;gap:8px;align-items:center;margin:4px 0;font-size:12.5px}
.taskbar .track{display:block;background:#e8eef7;border-radius:6px;height:12px;overflow:hidden}
.taskbar .fill{display:block;height:100%;border-radius:6px}
.issues{margin:8px 0 0;padding-left:18px;font-size:12.5px;color:#475569}
.issues li{margin:2px 0}
.links{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.links a{font-size:12.5px;color:#2563eb;text-decoration:none;border:1px solid #bfdbfe;background:#eff6ff;
padding:3px 9px;border-radius:8px}
.links a:hover{background:#dbeafe}
.kv{display:grid;grid-template-columns:112px 1fr;gap:5px 10px;font-size:12.8px}
.kv .k{color:var(--muted)}
.drift{margin-top:10px;border:1px solid #fecaca;background:#fef2f2;color:#b91c1c;border-radius:10px;padding:9px 11px;font-size:12.5px}
.warnbox{margin-top:10px;border:1px solid #fde68a;background:#fffbeb;color:#92400e;border-radius:10px;padding:9px 11px;font-size:12.5px}
footer{margin-top:22px;color:var(--muted);font-size:12px;line-height:1.9}
code{background:#eef2f7;border:1px solid #e2e8f0;border-radius:5px;padding:1px 5px;font-size:12px;
font-family:Consolas,"Courier New",monospace}
.empty{padding:34px;text-align:center;color:var(--muted)}
.nomap{color:var(--muted2)}
"""

JS = r"""
(function(){
  var META = __RESULT_META__;
  var rows = DATA.runs.slice();
  var sortKey = 'started', sortDir = -1, selected = null;

  function fmtDur(s){
    s = Math.round(s || 0);
    if (!s) return '-';
    var h = Math.floor(s/3600), m = Math.floor((s%3600)/60), x = s%60;
    if (h) return h+'h'+String(m).padStart(2,'0')+'m';
    if (m) return m+'m'+String(x).padStart(2,'0')+'s';
    return x+'s';
  }
  function badge(r){
    var meta = META[r] || {label:r, color:'#94a3b8', kind:'muted'};
    return '<span class="pill '+meta.kind+'">'+meta.label+'</span>';
  }
  function passes(r){
    if (!document.getElementById('showDry').checked && r.result === 'dry-run') return false;
    var res = document.getElementById('fResult').value;
    if (res && r.result !== res) return false;
    var mode = document.getElementById('fMode').value;
    if (mode && r.mode !== mode) return false;
    var from = document.getElementById('fFrom').value, to = document.getElementById('fTo').value;
    var day = (r.started || '').substring(0,10);
    if (from && day < from) return false;
    if (to && day > to) return false;
    var q = (document.getElementById('fSearch').value || '').trim().toLowerCase();
    if (q && (r.id + ' ' + (r.stage||'') + ' ' + r.result + ' ' + r.mode).toLowerCase().indexOf(q) < 0) return false;
    return true;
  }
  function cmp(a, b){
    var x = a[sortKey], y = b[sortKey];
    if (x === y) return 0;
    if (typeof x === 'number' && typeof y === 'number') return (x - y) * sortDir;
    return String(x).localeCompare(String(y)) * sortDir;
  }
  function render(){
    rows.sort(cmp);
    var body = document.querySelector('#runs tbody');
    var shown = rows.filter(passes);
    var html = shown.map(function(r){
      var stage = r.stage ? (r.stage + (r.stage_kind ? ' <span class="nomap">(' + (DATA.stageKind[r.stage_kind]||r.stage_kind) + ')</span>' : '')) : '<span class="nomap">-</span>';
      var issue = r.errs ? ('<b style="color:#b91c1c">' + r.errs + '</b> / ' + r.notices) : ('0 / ' + r.notices);
      return '<tr data-id="' + r.id + '" class="' + (selected===r.id?'sel':'') + '">'
        + '<td>' + (r.started || r.id) + '</td>'
        + '<td>' + (r.mode || '-') + '</td>'
        + '<td>' + badge(r.result) + '</td>'
        + '<td class="num">' + fmtDur(r.dur) + '</td>'
        + '<td class="num">' + r.done + '/' + r.total + '</td>'
        + '<td class="num">' + r.battles + '</td>'
        + '<td class="num">' + r.med + '</td>'
        + '<td class="num">' + issue + '</td>'
        + '<td>' + stage + (r.has_report ? '' : ' <span class="nomap">[无报告]</span>') + '</td>'
        + '<td><a href="../logs/' + r.id + '/summary.txt" onclick="event.stopPropagation()">摘要</a></td>'
        + '</tr>';
    }).join('');
    body.innerHTML = html || '<tr><td colspan="10" class="empty">没有符合条件的记录</td></tr>';
    document.getElementById('shownCount').textContent = shown.length;
    document.getElementById('totalCount').textContent = rows.length;
    Array.prototype.forEach.call(body.querySelectorAll('tr[data-id]'), function(tr){
      tr.addEventListener('click', function(){ select(tr.getAttribute('data-id')); });
    });
  }
  function select(id){
    selected = (selected === id) ? null : id;
    var box = document.getElementById('detail');
    if (!selected){ box.style.display = 'none'; render(); return; }
    var r = rows.filter(function(x){ return x.id === id; })[0];
    if (!r){ box.style.display = 'none'; return; }
    var maxDur = Math.max.apply(null, r.tasks.map(function(t){ return t.dur; }).concat([1]));
    var tasks = r.tasks.map(function(t){
      var meta = DATA.taskStatus[t.status] || {label:t.status, color:'#94a3b8'};
      var w = Math.max(1, Math.round(t.dur / maxDur * 100));
      return '<div class="taskbar"><span>' + t.label + '</span>'
        + '<span class="track"><span class="fill" style="width:' + w + '%;background:' + meta.color + '"></span></span>'
        + '<span class="num">' + fmtDur(t.dur) + ' · ' + meta.label + '</span></div>';
    }).join('') || '<div class="nomap">该次没有任务明细</div>';
    var fatal = r.issues.filter(function(i){ return i.fatal; });
    var soft = r.issues.filter(function(i){ return !i.fatal; });
    var shown = fatal.slice(0,6).concat(soft.slice(0,6));
    var issues = shown.map(function(i){
      return '<li>' + (i.fatal ? '<b style="color:#b91c1c">[错误]</b>' : '[提示]') + ' ' + i.kind + ' ' + i.msg + '</li>';
    }).join('');
    var hidden = r.issues.length - shown.length;
    if (hidden > 0) { issues += '<li class="nomap">…另有 ' + hidden + ' 条提示未显示</li>'; }
    var drops = Object.keys(r.drops || {}).map(function(stage){
      var items = Object.keys(r.drops[stage]).map(function(name){
        var d = r.drops[stage][name];
        return name + ' ' + d.total + (d.added ? '(+' + d.added + ')' : '');
      }).join('、');
      return '<div>' + stage + '：' + items + '</div>';
    }).join('');
    var warns = (r.warnings || []).map(function(w){ return '<li>' + w + '</li>'; }).join('');
    box.innerHTML = '<h3>' + r.id + ' · ' + badge(r.result) + '</h3>'
      + '<div class="meta">' + (r.started||'') + ' → ' + (r.finished||'') + ' · 耗时 ' + fmtDur(r.dur)
      + ' · 任务 ' + r.done + '/' + r.total + ' · 作战 ' + r.battles + ' · 理智药 ' + r.med + ' · 碎石 ' + r.stone + '</div>'
      + '<div class="grid2"><div><b style="font-size:12.5px">任务明细</b>' + tasks + '</div>'
      + '<div>' + (issues ? '<b style="font-size:12.5px">错误与提示</b><ul class="issues">' + issues + '</ul>' : '')
      + (warns ? '<b style="font-size:12.5px">警告</b><ul class="issues">' + warns + '</ul>' : '')
      + (drops ? '<b style="font-size:12.5px">掉落</b><div style="font-size:12.5px;margin-top:4px">' + drops + '</div>' : '') + '</div></div>'
      + '<div class="links"><a href="../logs/' + r.id + '/summary.txt">摘要</a>'
      + '<a href="../logs/' + r.id + '/report.json">报告 JSON</a>'
      + '<a href="../logs/' + r.id + '/run.log">运行日志</a>'
      + '<a href="../logs/' + r.id + '/callbacks.jsonl">原始回调</a></div>';
    box.style.display = 'block';
    render();
    box.scrollIntoView({behavior:'smooth', block:'nearest'});
  }
  Array.prototype.forEach.call(document.querySelectorAll('th[data-key]'), function(th){
    th.addEventListener('click', function(){
      var key = th.getAttribute('data-key');
      if (sortKey === key) { sortDir = -sortDir; } else { sortKey = key; sortDir = -1; }
      Array.prototype.forEach.call(document.querySelectorAll('th .arw'), function(a){ a.textContent = ''; });
      var arw = th.querySelector('.arw');
      if (arw) arw.textContent = sortDir < 0 ? '▼' : '▲';
      render();
    });
  });
  ['fResult','fMode','fFrom','fTo','fSearch','showDry'].forEach(function(id){
    var el = document.getElementById(id);
    if (el) el.addEventListener('change', render);
    if (el && id === 'fSearch') el.addEventListener('input', render);
  });
  document.getElementById('btnReset').addEventListener('click', function(){
    document.getElementById('fResult').value = '';
    document.getElementById('fMode').value = '';
    document.getElementById('fFrom').value = '';
    document.getElementById('fTo').value = '';
    document.getElementById('fSearch').value = '';
    document.getElementById('showDry').checked = DATA.showDryDefault;
    selected = null;
    document.getElementById('detail').style.display = 'none';
    render();
  });
  render();
  var m = (location.hash || '').match(/^#run=(.+)$/);
  if (m) { select(decodeURIComponent(m[1])); }
})();
"""


def kpi_card(label: str, value: str, hint: str = "", accent: str = "") -> str:
    style = f' style="color:{accent}"' if accent else ""
    return (
        '<div class="card">'
        f'<div class="k">{esc(label)}</div>'
        f'<div class="v"{style}>{esc(value)}</div>'
        f'<div class="h">{esc(hint)}</div>'
        "</div>"
    )


def render_kpis(agg: dict, task_state: dict | None) -> str:
    totals = agg["totals"]
    latest = agg["latest"] or {}
    rate = (totals["success"] / totals["runs"] * 100.0) if totals["runs"] else 0.0
    latest_value = result_label(latest.get("result", "")) if latest else "无记录"
    latest_hint = f"{latest.get('started', '')} · {fmt_duration(latest.get('dur'))}" if latest else "还没有运行记录"
    next_run = "未注册"
    next_hint = ""
    if task_state:
        next_run = fmt_ts(task_state.get("next")) if task_state.get("next") else "未注册"
        triggers = task_state.get("triggers") or []
        if isinstance(triggers, str):
            triggers = [triggers]
        next_hint = " / ".join(triggers[:4])
    cards = [
        kpi_card("真实运行次数", str(totals["runs"]), f"另有 {agg['dry_run_count']} 次干跑自检（默认不显示）"),
        kpi_card("成功率", f"{rate:.0f}%", f"成功 {totals['success']} · 部分 {totals['partial']} · 失败 {totals['bad']} · 环境 {totals['env_error']}", result_color("success")),
        kpi_card("平均耗时", fmt_duration(totals["avg_duration"]), f"累计 {fmt_duration(totals['duration'])}"),
        kpi_card("最近一次", latest_value, latest_hint, result_color(latest.get("result", "")) if latest else ""),
        kpi_card("累计作战 / 理智药", f"{totals['battles']} / {totals['medicine']}", f"碎石 {totals['stone']}"),
        kpi_card("下次定时运行", next_run, next_hint),
    ]
    return '<div class="cards">' + "".join(cards) + "</div>"


def render_table(rows_total: int, show_dry: bool) -> str:
    headers = [
        ("started", "开始时间", ""),
        ("mode", "模式", ""),
        ("result", "结果", ""),
        ("dur", "耗时", "num"),
        ("done", "任务", "num"),
        ("battles", "作战", "num"),
        ("med", "用药", "num"),
        ("errs", "错误/提示", "num"),
        ("stage", "关卡", ""),
    ]
    head = "".join(
        f'<th data-key="{key}"{" class=\"" + cls + "\"" if cls else ""}>{label}<span class="arw"></span></th>'
        for key, label, cls in headers
    )
    head += "<th>报告</th>"
    show_dry_attr = " checked" if show_dry else ""
    return f"""
<div class="panel">
  <h2>运行记录 <span class="hint">显示 <b id="shownCount">0</b> / <b id="totalCount">{rows_total}</b> 条 · 点击表头排序 · 点击行看详情</span></h2>
  <div class="toolbar">
    <select id="fResult">
      <option value="">全部结果</option>
      <option value="success">成功</option>
      <option value="partial">部分完成</option>
      <option value="failed">失败</option>
      <option value="env-error">环境失败</option>
      <option value="aborted">被中断</option>
      <option value="timeout">超时</option>
    </select>
    <select id="fMode">
      <option value="">全部模式</option>
      <option value="scheduled">scheduled（定时）</option>
      <option value="manual">manual（手动）</option>
      <option value="dry-run">dry-run（干跑）</option>
    </select>
    <input type="date" id="fFrom" title="起始日期">
    <input type="date" id="fTo" title="结束日期">
    <input type="text" id="fSearch" placeholder="搜索 run_id / 关卡 / 结果…" style="min-width:180px">
    <label><input type="checkbox" id="showDry"{show_dry_attr}> 显示干跑记录</label>
    <button id="btnReset" style="padding:5px 10px;border:1px solid #e2e8f0;border-radius:8px;background:#fff;cursor:pointer">重置</button>
  </div>
  <div class="scroll">
    <table id="runs">
      <thead><tr>{head}</tr></thead>
      <tbody></tbody>
    </table>
  </div>
  <div id="detail" class="detail" style="display:none"></div>
</div>
"""


def render_health(cfg: dict, task_state: dict | None, agg: dict) -> str:
    rows: list[str] = []
    drift = ""

    schedule = cfg.get("schedule") or {}
    configured = [str(t) for t in (schedule.get("times") or [])]
    task_name = str(schedule.get("task_name") or "MAA-Arknights-Auto")

    if task_state:
        triggers = task_state.get("triggers") or []
        if isinstance(triggers, str):
            triggers = [triggers]
        triggers = [str(t) for t in triggers]
        code = task_state.get("last_result")
        try:
            code_int = int(code)
        except (TypeError, ValueError):
            code_int = -1
        rows += [
            ("计划任务", f"{task_name}（{task_state.get('state') or '?'}）"),
            ("注册触发", " / ".join(triggers) if triggers else "未读到"),
            ("下次运行", fmt_ts(task_state.get("next"))),
            ("上次运行", fmt_ts(task_state.get("last"))),
            ("上次结果", f"{TASK_RESULT_CODE.get(code_int, '未知')}（{code_int}）"),
            ("错过次数", str(task_state.get("missed", 0))),
        ]
        if configured and triggers and sorted(configured) != sorted(triggers):
            drift = (
                '<div class="drift"><b>⚠ 配置与计划任务不一致</b><br>'
                f"config.json 写的是 <code>{esc(' / '.join(configured))}</code>，"
                f"计划任务实际触发的是 <code>{esc(' / '.join(triggers))}</code>。<br>"
                "改时间后必须再执行 <code>.\\register_schedule.ps1</code> 才会生效。</div>"
            )
    else:
        rows.append(("计划任务", "查询失败或未注册"))

    tasks_cfg = cfg.get("tasks") or {}
    overrides = tasks_cfg.get("stage_overrides") or {}
    run_cfg = cfg.get("run") or {}
    rows += [
        ("任务队列来源", f"tasks.source = {tasks_cfg.get('source') or 'gui'}（跟随 MAA 界面）"),
        (
            "关卡",
            "跟随 MAA 界面"
            if not overrides
            else "强制锁定：" + "、".join(f"{k}→{v}" for k, v in overrides.items()),
        ),
        ("关卡校验", f"verify_stage={run_cfg.get('verify_stage')} · 失效策略={run_cfg.get('on_invalid_stage')}"),
        ("防休眠", str(run_cfg.get("prevent_sleep"))),
        ("模拟器启动重试", f"{cfg.get('emulator', {}).get('launch_retry', 3)} 次"),
        ("报告保留", f"{cfg.get('report', {}).get('keep_days', 60)} 天"),
    ]

    kv = "".join(f'<div class="k">{esc(k)}</div><div>{esc(v)}</div>' for k, v in rows)

    issues = agg.get("issues_top") or []
    if issues:
        issue_html = "".join(
            f'<li>{"<b style=\'color:#b91c1c\'>[错误]</b>" if item["fatal"] else "[提示]"} '
            f'{esc(item["kind"])} {esc(item["msg"])} <span class="nomap">×{item["n"]}</span></li>'
            for item in issues
        )
        issues_html = f'<ul class="issues">{issue_html}</ul>'
    else:
        issues_html = '<div class="nomap" style="font-size:12.5px">没有记录到错误或提示</div>'

    return (
        '<div class="grid2">'
        '<div class="panel"><h2>定时与配置 <span class="hint">数据来自本地计划任务与 config.json</span></h2>'
        f'<div class="kv">{kv}</div>{drift}</div>'
        f'<div class="panel"><h2>错误 / 提示归因 <span class="hint">按出现次数排序 · 提示不影响运行结论</span></h2>{issues_html}</div>'
        "</div>"
    )


def render_footer(
    bad_lines: int,
    truncated: int,
    total_runs: int,
    generated: str,
    elapsed_ms: int,
    conflicts: dict | None = None,
) -> str:
    bits = [f"生成于 {esc(generated)}（{elapsed_ms} ms）"]
    conflicts = conflicts or {}
    if conflicts.get("total"):
        bits.append(
            f"并发冲突 {conflicts['total']} 次（今日 {conflicts.get('today', 0)} 次，"
            "见 logs\\lock-conflicts.log）"
        )
    if truncated:
        bits.append(f"仅统计最近 {total_runs} 条记录（受 max_runs 限制）")
    if bad_lines:
        bits.append(f"{bad_lines} 行索引解析失败已跳过")
    return f"""
<footer>
  {' · '.join(bits)}<br>
  本页面是静态只读文件，刷新/重建才能看到最新数据。常用命令（在工程目录里执行）：<br>
  <code>.\\dashboard.ps1 -Open</code> 重建并打开 &nbsp;·&nbsp;
  <code>.\\schedule_status.ps1</code> 查看定时状态 &nbsp;·&nbsp;
  <code>.\\run_now.ps1</code> 立即跑一次 &nbsp;·&nbsp;
  <code>Disable-ScheduledTask -TaskName '{esc('MAA-Arknights-Auto')}'</code> 暂停定时
</footer>
"""


def render_html(
    cfg: dict,
    records: list[dict],
    agg: dict,
    task_state: dict | None,
    generated: str,
    show_dry: bool,
    bad_lines: int,
    truncated: int,
    total_runs: int,
    elapsed_ms: int,
    conflicts: dict | None = None,
) -> str:
    if not records:
        body_main = (
            '<div class="panel"><div class="empty">还没有任何运行记录。<br><br>'
            "先执行 <code>.\\run_now.ps1 -DryRun</code> 生成一次自检记录，"
            "或等定时任务跑一次。<br>记录来自 <code>logs\\runs.jsonl</code>。</div></div>"
        )
    else:
        charts = (
            '<div class="panel"><h2>按日运行结果 <span class="hint">统计口径：'
            + ("含干跑" if show_dry else "仅真实运行")
            + '</span></h2>'
            + svg_daily_bars(agg["by_day"])
            + "</div>"
            '<div class="grid2">'
            '<div class="panel"><h2>每次运行耗时 <span class="hint">分钟</span></h2>'
            + svg_duration_line(agg["durations"])
            + "</div>"
            '<div class="panel"><h2>任务链平均耗时 <span class="hint">横条越长越慢</span></h2>'
            + svg_task_bars(agg["task_avg"])
            + "</div>"
            "</div>"
        )
        body_main = render_kpis(agg, task_state) + charts + render_table(len(records), show_dry) + render_health(cfg, task_state, agg)

    meta_js = {
        key: {"label": label, "color": color, "kind": kind}
        for key, (label, color, kind) in RESULT_META.items()
    }
    task_status_js = {key: {"label": label, "color": color} for key, (label, color) in TASK_STATUS.items()}
    payload = {
        "runs": records,
        "showDryDefault": show_dry,
        "stageKind": STAGE_KIND,
        "taskStatus": task_status_js,
        "generatedAt": generated,
    }
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    js = JS.replace("__RESULT_META__", json.dumps(meta_js, ensure_ascii=False))

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MAA 挂机看板 · {esc(generated[:16])}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="top">
    <div>
      <h1>MAA × 雷电 × 明日方舟 · 运行看板</h1>
      <div class="sub">数据来源：logs\\runs.jsonl 与各次 report.json · 生成于 {esc(generated)}</div>
    </div>
    <div class="sub">共 {len(records)} 条记录（含干跑）</div>
  </header>
  {body_main}
  {render_footer(bad_lines, truncated, total_runs, generated, elapsed_ms, conflicts)}
</div>
<script>
const DATA = {data_json};
{js}
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成 MAA 自动挂机可视化看板（单文件 HTML）")
    parser.add_argument("--config", default=str(ROOT / "config.json"), help="配置文件路径")
    parser.add_argument("--logs", default="", help="日志目录（默认取 config 的 report.dir）")
    parser.add_argument("--out", default="", help="输出 HTML 路径（默认 <dashboard>/index.html）")
    parser.add_argument("--include-dry-run", action="store_true", help="图表也统计干跑记录")
    parser.add_argument("--max-runs", type=int, default=0, help="最多读取最近 N 条（0=用配置值）")
    parser.add_argument("--no-task-query", action="store_true", help="不查询 Windows 计划任务")
    parser.add_argument("--self-check", action="store_true", help="构建到临时目录并校验结构")
    parser.add_argument("--quiet", action="store_true", help="只输出错误")
    return parser.parse_args(argv)


def build(args: argparse.Namespace, out_dir: Path) -> tuple[Path, dict]:
    started = time.monotonic()
    cfg = mc.load_config(Path(args.config))
    dashboard_cfg = cfg.get("dashboard") or {}

    logs_dir = Path(args.logs) if args.logs else mc.resolve_path(cfg, str((cfg.get("report") or {}).get("dir") or "logs"))
    max_runs = int(args.max_runs or dashboard_cfg.get("max_runs") or 500)
    show_dry = bool(args.include_dry_run or dashboard_cfg.get("include_dry_run_by_default"))

    records, bad_lines, total_runs = build_dataset(logs_dir, max_runs)
    agg = aggregate(records, charts_include_dry=show_dry)

    task_state = None
    if not args.no_task_query:
        task_state = query_task_state(str((cfg.get("schedule") or {}).get("task_name") or "MAA-Arknights-Auto"))

    conflicts = read_conflict_stats(logs_dir)
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elapsed_ms = int((time.monotonic() - started) * 1000)
    html_text = render_html(
        cfg=cfg,
        records=records,
        agg=agg,
        task_state=task_state,
        generated=generated,
        show_dry=show_dry,
        bad_lines=bad_lines,
        truncated=max(0, total_runs - len(records)),
        total_runs=len(records),
        elapsed_ms=elapsed_ms,
        conflicts=conflicts,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "index.html"
    out_file.write_text(html_text, encoding="utf-8")
    return out_file, {
        "records": len(records),
        "real": agg["totals"]["runs"],
        "dry": agg["dry_run_count"],
        "bytes": len(html_text.encode("utf-8")),
        "bad_lines": bad_lines,
        "task_state": bool(task_state),
        "ms": int((time.monotonic() - started) * 1000),
    }


def self_check(args: argparse.Namespace) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="maa-dash-"))
    try:
        out_file, info = build(args, tmp)
        text = out_file.read_text(encoding="utf-8")
        problems: list[str] = []
        if info["bytes"] < 5000:
            problems.append(f"HTML 太小（{info['bytes']} 字节）")
        if "const DATA =" not in text:
            problems.append("缺少内嵌数据 DATA")
        if "__RESULT_META__" in text:
            problems.append("存在未替换的占位符")
        start = text.find("const DATA = ") + len("const DATA = ")
        end = text.find(";\n", start)
        try:
            payload = json.loads(text[start:end])
            if not isinstance(payload.get("runs"), list):
                problems.append("DATA.runs 不是数组")
            elif len(payload["runs"]) != info["records"]:
                problems.append("嵌入记录数与统计不一致")
        except json.JSONDecodeError as exc:
            problems.append(f"内嵌 JSON 解析失败：{exc}")
        if "<svg" not in text and info["records"]:
            problems.append("没有生成图表")
        say("自检结果：")
        say(f"  记录 {info['records']} 条（真实 {info['real']} / 干跑 {info['dry']}），坏行 {info['bad_lines']}")
        say(f"  HTML {info['bytes'] / 1024:.1f} KB，生成耗时 {info['ms']} ms，计划任务查询 {'成功' if info['task_state'] else '跳过/失败'}")
        if problems:
            say("  [FAIL] " + "; ".join(problems))
            return 1
        say("  [OK] 结构校验通过")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.self_check:
            return self_check(args)
        out_dir = Path(args.out).parent if args.out else (ROOT / "dashboard")
        out_file, info = build(args, out_dir)
        if args.out:
            target = Path(args.out)
            target.write_text(out_file.read_text(encoding="utf-8"), encoding="utf-8")
            out_file = target
        if not args.quiet:
            say(f"看板已生成：{out_file}")
            say(
                f"  记录 {info['records']} 条（真实 {info['real']} / 干跑 {info['dry']}）· "
                f"{info['bytes'] / 1024:.1f} KB · {info['ms']} ms"
            )
        return 0
    except Exception as exc:  # noqa: BLE001 - 生成失败不应拖垮调用方
        if sys.stderr is not None:
            print(f"看板生成失败：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
