#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# 作者: leo <1193725859@qq.com> · 创建 2026-10-12 · 最近修改 2026-10-25
# 项目: maa-arknights-auto（MIT License） · 见 README.md / AUTHORS.md

"""MAA + 雷电模拟器 明日方舟 定时自动运行器。

一次运行的完整流程：
    0. 前置自检（MAA 目录 / MaaCore / 雷电 ldconsole / adb / 资源配置 / 任务映射）
    1. 启动雷电模拟器实例（已在运行则跳过）并等待 adb 与系统启动完成
    2. 读取 MAA GUI 配置，把任务队列翻译成 MaaCore 任务（「开始唤醒」即 Link Start）
    3. 通过 MAA 官方 Python 接口直调 MaaCore，连接设备并执行任务
    4. 全程收集回调：任务成败、掉落、公招、战斗次数、理智药/碎石消耗、错误
    5. 生成运行报告（report.json + summary.txt），保留原始回调流
    6. 收尾：停止 MAA、退出模拟器、清理残留进程

用法：
    python maa_runner.py --config config.json              # 立即运行一次
    python maa_runner.py --config config.json --dry-run    # 干跑：只自检 + 打印任务队列
"""

from __future__ import annotations

import argparse
import ctypes
import gc
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import maa_config as mc  # noqa: E402

# 退出码：写进报告，也方便计划任务里判断「上次运行结果」
EXIT_OK = 0
EXIT_PARTIAL = 2
EXIT_FAILED = 3
EXIT_ENV_ERROR = 4
EXIT_TIMEOUT = 5
EXIT_UNEXPECTED = 9
EXIT_LOCKED = 12          # 已有一次运行在进行（跨进程锁挡下，未启动任何东西）

RESULT_TEXT = {
    "success": "成功",
    "partial": "部分完成（有任务失败或未执行）",
    "failed": "失败",
    "aborted": "被中断",
    "timeout": "超时中断",
    "env-error": "环境自检失败",
    "dry-run": "干跑（未实际执行任务）",
    "unexpected": "未预期的异常",
}

_RESULT_EXIT = {
    "success": EXIT_OK,
    "dry-run": EXIT_OK,
    "partial": EXIT_PARTIAL,
    "failed": EXIT_FAILED,
    "aborted": EXIT_FAILED,
    "timeout": EXIT_TIMEOUT,
    "env-error": EXIT_ENV_ERROR,
    "unexpected": EXIT_UNEXPECTED,
}

CREATE_NO_WINDOW = 0x08000000

# SetThreadExecutionState 用的标志：运行期间别让系统自己睡过去
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

# MaaCore 回调消息编号（见 docs/zh-cn/protocol/callback-schema.md）
MSG_INTERNAL_ERROR = 0
MSG_INIT_FAILED = 1
MSG_CONNECTION_INFO = 2
MSG_ALL_TASKS_COMPLETED = 3
MSG_TASK_CHAIN_ERROR = 10000
MSG_TASK_CHAIN_START = 10001
MSG_TASK_CHAIN_COMPLETED = 10002
MSG_TASK_CHAIN_EXTRA_INFO = 10003
MSG_TASK_CHAIN_STOPPED = 10004
MSG_SUBTASK_ERROR = 20000
MSG_SUBTASK_START = 20001
MSG_SUBTASK_COMPLETED = 20002
MSG_SUBTASK_EXTRA_INFO = 20003
MSG_SUBTASK_STOPPED = 20004
MSG_REPORT_REQUEST = 30000

# 真正会让本次运行算「失败/部分完成」的问题类型；不在这里的都只作为「提示」
FATAL_ISSUE_KINDS = {
    "InternalError",
    "InitFailed",
    "TaskChainError",
    "Connection:ConnectFailed",
    "Connection:Disconnect",
    "Connection:ScreencapFailed",
    "Connection:UnsupportedResolution",
    "Connection:ResolutionError",
    "Connection:TouchModeNotAvailable",
}


# ======================================================================================
# 基础设施
# ======================================================================================


class Logger:
    """同时写控制台与文件的简单日志器。

    注意：计划任务会用 pythonw.exe 静默运行，此时 sys.stdout 为 None，
    所以打印前必须判空，否则第一行日志就会抛 AttributeError。
    """

    def __init__(self, log_file: Path | None = None):
        self._file = None
        self._lock = threading.Lock()
        self._echo = sys.stdout is not None
        if log_file is not None:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            self._file = log_file.open("a", encoding="utf-8")

    def _log(self, level: str, message: str) -> None:
        line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] [{level:<5}] {message}"
        with self._lock:
            if self._echo:
                try:
                    print(line, flush=True)
                except (OSError, ValueError, AttributeError):
                    self._echo = False
            if self._file is not None:
                self._file.write(line + "\n")
                self._file.flush()

    def info(self, message: str) -> None:
        self._log("INFO", message)

    def warn(self, message: str) -> None:
        self._log("WARN", message)

    def error(self, message: str) -> None:
        self._log("ERROR", message)

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None


def _decode(data: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def run_capture(cmd: list[str], timeout: float | None = 60) -> tuple[int, str]:
    """执行外部命令并捕获输出。

    输出走临时文件而不是管道：既避免中文编码问题，也避免受限环境下管道被拒绝。
    """
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=".out")
    handle.close()
    path = Path(handle.name)
    try:
        with path.open("wb") as sink:
            flags = CREATE_NO_WINDOW if os.name == "nt" else 0
            try:
                proc = subprocess.run(
                    cmd,
                    stdout=sink,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=flags,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                return -1, f"命令执行超时（{timeout}s）：{' '.join(cmd)}"
        return proc.returncode, _decode(path.read_bytes())
    except FileNotFoundError:
        return -2, f"找不到可执行文件：{cmd[0]}"
    except OSError as exc:
        return -3, f"执行失败：{cmd[0]} -> {exc}"
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


# ======================================================================================
# 雷电模拟器控制
# ======================================================================================


class Emulator:
    def __init__(self, cfg: dict, logger: Logger):
        emu = cfg["emulator"]
        self.ldconsole = mc.resolve_path(cfg, str(emu.get("ldconsole") or ""))
        self.index = int(emu.get("instance_index") or 0)
        self.auto_launch = bool(emu.get("auto_launch", True))
        self.launch_wait = int(emu.get("launch_wait_sec") or 150)
        self.launch_retry = max(1, int(emu.get("launch_retry") or 3))
        self.launch_retry_interval = int(emu.get("launch_retry_interval_sec") or 20)
        self.boot_settle = int(emu.get("boot_settle_sec") or 20)
        self.quit_after_run = bool(emu.get("quit_after_run", True))
        self.kill_residual = bool(emu.get("kill_residual_processes", True))
        self.residual_names = [str(x) for x in (emu.get("residual_process_names") or [])]
        self.adb_path = mc.resolve_path(cfg, str(cfg["maa"].get("adb_path") or ""))
        self.address = str(cfg["maa"].get("adb_address") or "emulator-5554")
        self.used_address = self.address
        self.was_running = False
        self.launched_by_us = False
        self.logger = logger

    # -------- ldconsole --------

    def _ld(self, *args: str, timeout: float = 60) -> tuple[int, str]:
        return run_capture([str(self.ldconsole), *args], timeout=timeout)

    def is_running(self) -> bool:
        rc, out = self._ld("isrunning", "--index", str(self.index), timeout=30)
        text = out.strip().lower()
        return rc == 0 and "running" in text and "not running" not in text

    def launch(self) -> bool:
        rc, out = self._ld("launch", "--index", str(self.index), timeout=120)
        if rc != 0:
            self.logger.error(f"ldconsole launch 失败（rc={rc}）：{out.strip()}")
            return False
        return True

    def quit(self) -> None:
        self.logger.info(f"退出雷电模拟器实例 {self.index}")
        rc, out = self._ld("quit", "--index", str(self.index), timeout=120)
        if rc != 0:
            self.logger.warn(f"ldconsole quit 返回 rc={rc}：{out.strip()}")
        for _ in range(20):
            if not self.is_running():
                return
            time.sleep(1.5)
        self.logger.warn("模拟器仍在运行，尝试强杀残留进程")
        self.kill_residual_processes()

    def kill_residual_processes(self) -> None:
        if not self.kill_residual:
            return
        for name in self.residual_names:
            rc, out = run_capture(["taskkill", "/IM", name, "/F", "/T"], timeout=60)
            if rc == 0:
                self.logger.info(f"已结束残留进程 {name}")
            elif "not found" not in out.lower() and "没有" not in out:
                self.logger.warn(f"taskkill {name} 返回 rc={rc}：{out.strip()[:200]}")

    # -------- adb --------

    def adb(self, *args: str, timeout: float = 60, with_serial: bool = True) -> tuple[int, str]:
        cmd = [str(self.adb_path)]
        if with_serial and self.used_address:
            cmd += ["-s", self.used_address]
        cmd += list(args)
        return run_capture(cmd, timeout=timeout)

    def devices(self) -> list[str]:
        rc, out = run_capture([str(self.adb_path), "devices"], timeout=60)
        if rc != 0:
            return []
        serials: list[str] = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                serials.append(parts[0])
        return serials

    def ensure_device(self, timeout: int) -> bool:
        """等待 adb 出现可用设备；配置的串号不在时自动退化到唯一的在线设备。"""
        run_capture([str(self.adb_path), "start-server"], timeout=60)
        deadline = time.monotonic() + timeout
        tried_connect = False
        while time.monotonic() < deadline:
            serials = self.devices()
            if self.address in serials:
                self.used_address = self.address
                return True
            if ":" in self.address and not tried_connect:
                tried_connect = True
                run_capture([str(self.adb_path), "connect", self.address], timeout=60)
                continue
            if serials:
                self.used_address = serials[0]
                self.logger.warn(f"adb 中没有配置的 {self.address}，改用在线设备 {self.used_address}")
                return True
            time.sleep(2)
        self.logger.error(f"等待 adb 设备超时（{timeout}s），当前设备：{self.devices()}")
        return False

    def wait_boot_completed(self, timeout: int) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rc, out = self.adb("shell", "getprop", "sys.boot_completed", timeout=30)
            if rc == 0 and out.strip().startswith("1"):
                return True
            time.sleep(3)
        self.logger.error("等待模拟器系统启动完成超时（sys.boot_completed 未就绪）")
        return False

    def package_installed(self, package: str) -> bool:
        rc, out = self.adb("shell", "pm", "list", "packages", timeout=60)
        if rc != 0:
            return False
        return any(line.strip() == f"package:{package}" for line in out.splitlines())

    def ensure_ready(self) -> bool:
        self.was_running = self.is_running()
        if self.was_running:
            self.logger.info(f"雷电模拟器实例 {self.index} 已在运行，跳过启动")
            if not self.ensure_device(self.launch_wait):
                return False
        else:
            if not self.auto_launch:
                self.logger.error("模拟器未运行，且配置里 auto_launch=false")
                return False
            # 刚从 Modern Standby（合盖待机）恢复时，ldconsole launch 可能返回成功却什么都没启动
            # （实测：150s 内 adb 一直没有设备、dnplayer 进程数为 0），所以这里做有限次重试：
            # 每次失败先清理残留（quit + 杀进程）再重来。
            ready = False
            for attempt in range(1, self.launch_retry + 1):
                self.logger.info(f"启动雷电模拟器实例 {self.index}（第 {attempt}/{self.launch_retry} 次）…")
                if self.launch():
                    self.launched_by_us = True
                wait = self.launch_wait if attempt == 1 else max(45, self.launch_wait // 2)
                if self.ensure_device(wait):
                    ready = True
                    break
                if attempt < self.launch_retry:
                    self.logger.warn(
                        f"  第 {attempt} 次启动后 {wait}s 内仍没有等到 adb 设备，清理残留后重试"
                    )
                    self._ld("quit", "--index", str(self.index), timeout=60)
                    self.kill_residual_processes()
                    time.sleep(self.launch_retry_interval)
            if not ready:
                self.logger.error(f"启动模拟器失败（已重试 {self.launch_retry} 次）")
                return False
        self.logger.info(f"adb 设备就绪：{self.used_address}")

        if not self.wait_boot_completed(self.launch_wait):
            return False
        self.logger.info("模拟器系统启动完成")

        if self.boot_settle > 0:
            self.logger.info(f"等待 {self.boot_settle}s 让系统稳定后交给 MAA …")
            time.sleep(self.boot_settle)
        return True

    def cleanup(self, keep: bool) -> None:
        if keep or not self.quit_after_run:
            self.logger.info("按配置保留模拟器运行状态")
            return
        self.quit()
        run_capture([str(self.adb_path), "kill-server"], timeout=30)


# ======================================================================================
# 回调记录器
# ======================================================================================


class RunRecorder:
    """把 MaaCore 回调整理成报告数据。回调线程里只做最轻量的工作。"""

    def __init__(self, logger: Logger, callbacks_file: Path | None, http_timeout: int = 15):
        self.logger = logger
        self.lock = threading.Lock()
        self._fh = callbacks_file.open("a", encoding="utf-8") if callbacks_file else None
        self.last_event_ts = time.monotonic()
        self.taskid_to_label: dict[int, str] = {}
        self.chains: dict[int, dict] = {}
        self.chain_order: list[int] = []
        self.drops: dict[str, dict[str, dict]] = {}
        self.drop_events: list[dict] = []
        self.battles_by_task: dict[int, int] = {}
        self.recruit: dict = {
            "results": [],
            "special_tags": [],
            "preserved_tags": [],
            "selected_tags": [],
            "refreshed": 0,
            "no_permit": 0,
            "errors": 0,
        }
        self.counters = {
            "battles": 0,
            "medicine_used": 0,
            "expiring_medicine_used": 0,
            "stone_used": 0,
            "recruit_confirmed": 0,
            "recruit_refreshed": 0,
            "expedite_used": 0,
        }
        self.infrast: dict = {"facilities": {}, "products": {}, "not_enough_staff": []}
        self.connection_events: list[dict] = []
        self.errors: list[dict] = []
        self.notices: list[dict] = []
        self.notes: list[str] = []
        self.uploads = {"total": 0, "ok": 0, "failed": 0, "by_subtask": {}}
        self.finished_tasks: list[int] = []
        self.all_completed = False

        self._upload_queue: "queue.Queue[dict | None]" = queue.Queue()
        self._upload_timeout = http_timeout
        self._uploader = threading.Thread(target=self._upload_worker, daemon=True)
        self._uploader.start()

    # -------- 回调入口 --------

    def handle(self, msg: int, raw: str) -> None:
        payload: dict = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = {"_raw": raw}

        with self.lock:
            self.last_event_ts = time.monotonic()
            if self._fh is not None:
                try:
                    self._fh.write(
                        json.dumps(
                            {"t": datetime.now().isoformat(timespec="seconds"), "msg": msg, "data": payload},
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                except (OSError, TypeError):
                    pass
            self._dispatch(msg, payload)

    # -------- 分发 --------

    def _record_issue(self, kind: str, payload: dict, message: str = "") -> None:
        """记录一条问题。

        致命的（连接失败、任务链出错、核心异常）进 errors，会拉低运行结论；
        可恢复的（例如信用商店「信用不足」触发的 CreditShoppingTask 子任务报错、
        公招识别重试等）进 notices，只作为提示，不影响结论——这与 MAA 图形界面的
        表现一致：这类消息只是一条日志，任务链本身仍然算完成。
        """
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "kind": kind,
            "message": message or str(payload.get("why") or payload.get("what") or ""),
        }
        if kind in FATAL_ISSUE_KINDS:
            self.errors.append(entry)
        else:
            self.notices.append(entry)

    def _chain(self, taskid: int) -> dict:
        if taskid not in self.chains:
            self.chains[taskid] = {
                "task_id": taskid,
                "label": self.taskid_to_label.get(taskid, f"task#{taskid}"),
                "status": "unknown",
                "started_at": None,
                "finished_at": None,
                "duration_sec": None,
                "error": None,
            }
            self.chain_order.append(taskid)
        return self.chains[taskid]

    def _dispatch(self, msg: int, payload: dict) -> None:
        taskchain = str(payload.get("taskchain") or "")
        taskid = payload.get("taskid")
        what = str(payload.get("what") or "")
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}

        if msg == MSG_INTERNAL_ERROR:
            self._record_issue("InternalError", payload, "MaaCore 内部错误")
            self.logger.error(f"MaaCore 内部错误：{json.dumps(payload, ensure_ascii=False)}")
            return

        if msg == MSG_INIT_FAILED:
            self._record_issue("InitFailed", payload)
            self.logger.error(f"MaaCore 初始化失败：{what} / {payload.get('why')}")
            return

        if msg == MSG_CONNECTION_INFO:
            entry = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "what": what,
                "why": str(payload.get("why") or ""),
                "uuid": str(payload.get("uuid") or ""),
            }
            self.connection_events.append(entry)
            if what in {"Connected", "UuidGot", "Reconnected", "ResolutionGot", "FastestWayToScreencap"}:
                self.logger.info(f"连接信息：{what} {entry['why']} {details if details else ''}".strip())
            elif what:
                self.logger.warn(f"连接信息：{what} {entry['why']} {details if details else ''}".strip())
                if what in {
                    "ConnectFailed",
                    "Disconnect",
                    "ScreencapFailed",
                    "UnsupportedResolution",
                    "ResolutionError",
                    "TouchModeNotAvailable",
                }:
                    self._record_issue(f"Connection:{what}", payload, entry["why"])
            return

        if msg == MSG_ALL_TASKS_COMPLETED:
            self.all_completed = True
            self.finished_tasks = [int(x) for x in (payload.get("finished_tasks") or []) if isinstance(x, int)]
            self.logger.info(f"MAA 全部任务完成（最后任务链 {taskchain}）")
            return

        if msg in (MSG_TASK_CHAIN_START, MSG_TASK_CHAIN_COMPLETED, MSG_TASK_CHAIN_ERROR, MSG_TASK_CHAIN_STOPPED):
            if not isinstance(taskid, int):
                return
            chain = self._chain(taskid)
            chain["taskchain"] = taskchain
            if msg == MSG_TASK_CHAIN_START:
                chain["status"] = "running"
                chain["started_at"] = datetime.now().isoformat(timespec="seconds")
                self.logger.info(f"  ▶ 任务开始：{chain['label']}")
            elif msg == MSG_TASK_CHAIN_COMPLETED:
                chain["status"] = "completed"
                chain["finished_at"] = datetime.now().isoformat(timespec="seconds")
                self.logger.info(f"  ✔ 任务完成：{chain['label']}")
            elif msg == MSG_TASK_CHAIN_ERROR:
                chain["status"] = "error"
                chain["finished_at"] = datetime.now().isoformat(timespec="seconds")
                raw_details = payload.get("details")
                chain["error"] = raw_details if isinstance(raw_details, str) else json.dumps(
                    raw_details or {}, ensure_ascii=False
                )
                self._record_issue("TaskChainError", payload, f"{chain['label']} 任务链出错")
                self.logger.error(f"  ✘ 任务出错：{chain['label']} {chain['error']}")
            else:
                chain["status"] = "stopped"
                chain["finished_at"] = datetime.now().isoformat(timespec="seconds")
                self.logger.warn(f"  ■ 任务被停止：{chain['label']}")
            self._fill_duration(chain)
            return

        if msg == MSG_SUBTASK_START:
            self._on_subtask(details, taskid if isinstance(taskid, int) else None)
            return

        if msg == MSG_SUBTASK_ERROR:
            self._record_issue("SubTaskError", payload, str(payload.get("subtask") or ""))
            return

        if msg == MSG_SUBTASK_EXTRA_INFO:
            self._on_extra_info(taskchain, what, details, taskid if isinstance(taskid, int) else None)
            return

        if msg == MSG_REPORT_REQUEST:
            self._on_report_request(payload)
            return

    @staticmethod
    def _fill_duration(chain: dict) -> None:
        if chain.get("started_at") and chain.get("finished_at"):
            try:
                start = datetime.fromisoformat(chain["started_at"])
                end = datetime.fromisoformat(chain["finished_at"])
                chain["duration_sec"] = round((end - start).total_seconds(), 1)
            except ValueError:
                pass

    def _on_subtask(self, details: dict, taskid: int | None = None) -> None:
        task = str(details.get("task") or "")
        if task in {"StartButton2", "AnnihilationConfirm"}:
            self.counters["battles"] += 1
            if isinstance(taskid, int):
                self.battles_by_task[taskid] = self.battles_by_task.get(taskid, 0) + 1
        elif task == "MedicineConfirm":
            self.counters["medicine_used"] += 1
        elif task == "ExpiringMedicineConfirm":
            self.counters["expiring_medicine_used"] += 1
        elif task == "StoneConfirm":
            self.counters["stone_used"] += 1
        elif task == "RecruitConfirm":
            self.counters["recruit_confirmed"] += 1
        elif task == "RecruitRefreshConfirm":
            self.counters["recruit_refreshed"] += 1
        elif task == "RecruitNowConfirm":
            self.counters["expedite_used"] += 1

    def _on_extra_info(
        self, taskchain: str, what: str, details: dict, taskid: int | None = None
    ) -> None:
        if what == "StageDrops":
            stage = details.get("stage") if isinstance(details.get("stage"), dict) else {}
            code = str(stage.get("stageCode") or "?")
            bucket = self.drops.setdefault(code, {})
            for item in details.get("stats") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("itemName") or item.get("itemId") or "?")
                bucket[name] = {
                    "item_id": str(item.get("itemId") or ""),
                    "total": int(item.get("quantity") or 0),
                    "added": int(item.get("addQuantity") or 0),
                }
            self.drop_events.append(
                {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "taskid": taskid,
                    "stage": code,
                    "stars": details.get("stars"),
                    "cur_times": details.get("cur_times"),
                    "drops": details.get("drops") or [],
                }
            )
            return

        if what in {
            "RecruitResult",
            "RecruitTagsDetected",
            "RecruitSpecialTag",
            "RecruitPreservedTag",
            "RecruitTagsSelected",
            "RecruitTagsRefreshed",
            "RecruitNoPermit",
            "RecruitError",
            "RecruitPermitReserved",
            "RecruitPermitCountRecognitionFailed",
        }:
            self._on_recruit(what, details)
            return

        if what == "EnterFacility":
            facility = str(details.get("facility") or "?")
            self.infrast["facilities"][facility] = self.infrast["facilities"].get(facility, 0) + 1
            return

        if what == "ProductOfFacility":
            product = str(details.get("product") or "?")
            self.infrast["products"][product] = self.infrast["products"].get(product, 0) + 1
            return

        if what == "NotEnoughStaff":
            self.infrast["not_enough_staff"].append(
                {"facility": details.get("facility"), "index": details.get("index")}
            )
            self.notes.append(f"基建可用干员不足：{details.get('facility')} #{details.get('index')}")
            return

        if what in {"StageInfoError", "UnsupportedLevel"}:
            self.notes.append(f"{what}：{json.dumps(details, ensure_ascii=False)}")
            self._record_issue(what, {"details": details}, what)
            return

        if what == "ExceededLimit":
            self.notes.append(f"识别次数达到上限：{json.dumps(details, ensure_ascii=False)}")
            return

    def _on_recruit(self, what: str, details: dict) -> None:
        if what == "RecruitResult":
            self.recruit["results"].append(
                {
                    "level": details.get("level"),
                    "tags": details.get("tags") or [],
                    "result": details.get("result") or [],
                }
            )
        elif what == "RecruitSpecialTag":
            tag = str(details.get("tag") or "")
            if tag:
                self.recruit["special_tags"].append(tag)
        elif what == "RecruitPreservedTag":
            tag = str(details.get("tag") or "")
            if tag:
                self.recruit["preserved_tags"].append(tag)
        elif what == "RecruitTagsSelected":
            self.recruit["selected_tags"].append(details.get("tags") or [])
        elif what == "RecruitTagsRefreshed":
            self.recruit["refreshed"] += 1
        elif what == "RecruitNoPermit":
            self.recruit["no_permit"] += 1
        elif what in {"RecruitError", "RecruitPermitCountRecognitionFailed"}:
            self.recruit["errors"] += 1
            self._record_issue(what, {"details": details}, what)

    def _on_report_request(self, payload: dict) -> None:
        url = str(payload.get("url") or "")
        self.uploads["total"] += 1
        if not url:
            return
        self._upload_queue.put(
            {
                "url": url,
                "headers": payload.get("headers") or {},
                "body": payload.get("body") or "",
                "subtask": str(payload.get("subtask") or ""),
            }
        )

    def _upload_worker(self) -> None:
        """代 GUI 完成第三方上报（企鹅物流 / 一图流）。失败只记录，不影响任务。"""
        while True:
            job = self._upload_queue.get()
            if job is None:
                return
            subtask = job.get("subtask") or "unknown"
            headers = {str(k): str(v) for k, v in job["headers"].items()}
            headers.setdefault("Content-Type", "application/json")
            request = urllib.request.Request(
                job["url"], data=job["body"].encode("utf-8"), headers=headers, method="POST"
            )
            try:
                with urllib.request.urlopen(request, timeout=self._upload_timeout) as response:
                    ok = 200 <= response.status < 300
                with self.lock:
                    self.uploads["ok" if ok else "failed"] += 1
                    bucket = self.uploads["by_subtask"].setdefault(subtask, {"ok": 0, "failed": 0})
                    bucket["ok" if ok else "failed"] += 1
            except (urllib.error.URLError, OSError, ValueError) as exc:
                with self.lock:
                    self.uploads["failed"] += 1
                    bucket = self.uploads["by_subtask"].setdefault(subtask, {"ok": 0, "failed": 0})
                    bucket["failed"] += 1
                self.logger.warn(f"第三方上报失败（{subtask}）：{exc}")

    def close(self) -> None:
        self._upload_queue.put(None)
        # 给还没发完的第三方上报留点时间，别被后面的硬退出截断
        self._uploader.join(timeout=20)
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "chains": [dict(self.chains[tid]) for tid in self.chain_order],
                "drops": json.loads(json.dumps(self.drops, ensure_ascii=False)),
                "drop_events": list(self.drop_events),
                "battles_by_task": dict(self.battles_by_task),
                "recruit": json.loads(json.dumps(self.recruit, ensure_ascii=False)),
                "counters": dict(self.counters),
                "infrast": json.loads(json.dumps(self.infrast, ensure_ascii=False)),
                "connection_events": list(self.connection_events),
                "errors": list(self.errors),
                "notices": list(self.notices),
                "notes": list(self.notes),
                "uploads": json.loads(json.dumps(self.uploads, ensure_ascii=False)),
                "all_completed": self.all_completed,
                "finished_tasks": list(self.finished_tasks),
            }


def empty_snapshot() -> dict:
    return {
        "chains": [],
        "drops": {},
        "drop_events": [],
        "battles_by_task": {},
        "recruit": {
            "results": [],
            "special_tags": [],
            "preserved_tags": [],
            "selected_tags": [],
            "refreshed": 0,
            "no_permit": 0,
            "errors": 0,
        },
        "counters": {
            "battles": 0,
            "medicine_used": 0,
            "expiring_medicine_used": 0,
            "stone_used": 0,
            "recruit_confirmed": 0,
            "recruit_refreshed": 0,
            "expedite_used": 0,
        },
        "infrast": {"facilities": {}, "products": {}, "not_enough_staff": []},
        "connection_events": [],
        "errors": [],
        "notices": [],
        "notes": [],
        "uploads": {"total": 0, "ok": 0, "failed": 0, "by_subtask": {}},
        "all_completed": False,
        "finished_tasks": [],
    }


# ======================================================================================
# MaaCore 装载
# ======================================================================================


def load_maa_core(install_dir: Path, user_dir: Path, logger: Logger):
    """加载 MaaCore.dll 与资源，返回 (Asst, InstanceOptionType, ok)。

    资源加载顺序与 MAA GUI 保持一致：
      1. 主资源 <install>/resource（基础关卡、模板）
      2. 增量资源 <install>/cache（联网下载的活动关卡 tasks.json 等；缺了它活动关卡就导航不了）
    """
    python_dir = install_dir / "Python"
    if python_dir.is_dir() and str(python_dir) not in sys.path:
        sys.path.insert(0, str(python_dir))

    if os.name == "nt" and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(str(install_dir))
        except OSError:
            pass

    from asst.asst import Asst  # type: ignore
    from asst.utils import InstanceOptionType  # type: ignore

    user_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = install_dir / "cache"
    incremental = cache_dir if (cache_dir / "resource").is_dir() else None

    ok = Asst.load(path=install_dir, incremental_path=incremental, user_dir=user_dir)
    logger.info(
        f"MaaCore 资源加载：{'成功' if ok else '失败'}（主资源 {install_dir}"
        + (f"，增量资源 {incremental}" if incremental else "，未找到增量资源 cache 目录")
        + "）"
    )
    return Asst, InstanceOptionType, ok


def make_callback(Asst, recorder: RunRecorder):
    @Asst.CallBackType
    def _callback(msg, details, arg):  # noqa: ANN001
        try:
            raw = details.decode("utf-8", "replace") if details else "{}"
        except Exception:  # pragma: no cover
            raw = "{}"
        try:
            recorder.handle(int(msg), raw)
        except Exception:  # pragma: no cover - 回调线程绝不能抛异常
            pass

    return _callback


# ======================================================================================
# 主运行器
# ======================================================================================


class RunLock:
    """跨进程单实例锁：同一时刻只允许一个运行器。

    为什么需要：计划任务的 MultipleInstances=IgnoreNew 只约束"计划任务自己撞自己"，
    挡不住「定时触发」与「手动 run_now.ps1 / 双击快捷方式」同时发生。
    两个运行器抢同一台模拟器会互相 quit、adb 端口互踢，最终两边都失败。

    锁文件：`<logs>/.run.lock`（JSON：pid / 开始时间 / 模式 / host）。
    被挡下的那次不写 run 目录、不进 runs.jsonl，只追加一行到 lock-conflicts.log，
    这样看板与统计不会被"没跑成的触发"污染。
    """

    def __init__(self, cfg: dict, args: argparse.Namespace):
        self.logs_dir = mc.resolve_path(cfg, str((cfg.get("report") or {}).get("dir") or "logs"))
        self.path = self.logs_dir / ".run.lock"
        self.conflict_log = self.logs_dir / "lock-conflicts.log"
        self.mode = "dry-run" if getattr(args, "dry_run", False) else getattr(args, "mode", "manual")
        self.force = bool(getattr(args, "ignore_lock", False))
        self.max_minutes = int((cfg.get("run") or {}).get("task_timeout_minutes") or 300)
        self.owned = False
        self.holder: dict = {}

    # ---- 内部工具 ----

    @staticmethod
    def _announce(message: str) -> None:
        if sys.stderr is not None:
            try:
                print(message, file=sys.stderr, flush=True)
            except (OSError, ValueError):
                pass

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """判断 pid 是否仍然存活（Windows 用 OpenProcess + WaitForSingleObject，避免 pid 复用误判）。"""
        if not isinstance(pid, int) or pid <= 0:
            return False
        if os.name != "nt":
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x101000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT = 仍在运行
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            return True  # 无法判断时按"活着"处理，宁可不抢

    def _read_holder(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _is_stale(self, holder: dict) -> bool:
        if not holder:
            # 文件存在但读不懂（BOM/损坏/字段缺失）：新近的坏锁按"有人在用"处理，宁可不抢
            try:
                age = time.time() - self.path.stat().st_mtime
            except OSError:
                return False
            return age > 600
        pid = holder.get("pid")
        if not self._pid_alive(int(pid) if isinstance(pid, (int, str)) and str(pid).isdigit() else 0):
            return True
        try:
            started = datetime.strptime(str(holder.get("started_at") or ""), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False
        return (datetime.now() - started).total_seconds() > (self.max_minutes + 30) * 60

    def _write_conflict(self, holder: dict) -> None:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if holder:
            detail = (
                f"（pid={holder.get('pid')}，开始于 {holder.get('started_at')}，"
                f"mode={holder.get('mode')}，host={holder.get('host')}）"
            )
        else:
            detail = "（锁文件无法解析，可能正被另一个进程写入）"
        line = f"[{stamp}] 本次触发（mode={self.mode}）被挡下：已有运行在进行{detail}\n"
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            with self.conflict_log.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass

    # ---- 对外 ----

    def acquire(self) -> bool:
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self._read_holder()
                if self.force:
                    self._announce(f"[WARN] --ignore-lock：忽略现有锁（pid={holder.get('pid')}）")
                elif self._is_stale(holder):
                    self._announce(
                        f"[WARN] 发现残留锁（pid={holder.get('pid')} 已不存在或已超时），本次接管"
                    )
                else:
                    self.holder = holder
                    self._write_conflict(holder)
                    self._announce(
                        "[ERROR] 已有一次运行在进行，本次不启动："
                        f" pid={holder.get('pid')} 开始于 {holder.get('started_at')}"
                        f"（mode={holder.get('mode')}）。"
                        " 如需强制并行（不建议），用 --ignore-lock。"
                    )
                    return False
                try:
                    self.path.unlink()
                except OSError:
                    pass
                if attempt == 2:
                    self._announce("[ERROR] 锁文件无法清理，放弃本次运行")
                    return False
                continue
            except OSError as exc:
                self._announce(f"[ERROR] 无法创建锁文件 {self.path}：{exc}")
                return False

            info = {
                "pid": os.getpid(),
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "mode": self.mode,
                "host": os.environ.get("COMPUTERNAME", ""),
            }
            try:
                os.write(fd, json.dumps(info, ensure_ascii=False).encode("utf-8"))
            finally:
                os.close(fd)
            self.owned = True
            return True
        return False

    def release(self) -> None:
        if not self.owned:
            return
        try:
            holder = self._read_holder()
            if holder.get("pid") == os.getpid():
                self.path.unlink()
        except OSError:
            pass
        finally:
            self.owned = False


class Runner:
    def __init__(self, args: argparse.Namespace, cfg: dict):
        self.args = args
        self.cfg = cfg
        self.mode = "dry-run" if args.dry_run else args.mode
        self.dry_run = self.mode == "dry-run"

        report_dir = mc.resolve_path(cfg, str(cfg["report"]["dir"]))
        report_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir = report_dir
        self.run_id = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.run_dir = report_dir / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.logger = Logger(self.run_dir / "run.log")
        self.started = datetime.now()
        self.t0 = time.monotonic()

        self.warnings: list[str] = []
        self.preflight_problems: list[str] = []
        self.abort_reason: str | None = None
        self.tasks: list[mc.TaskSpec] = []
        self.gui_cfg: dict | None = None
        self.task_entries: list[dict] = []
        self.env_info: dict = {}
        self.emulator: Emulator | None = None
        self.recorder: RunRecorder | None = None
        self.asst_cls = None
        self.instance_option = None
        self.install_dir = mc.resolve_path(cfg, str(cfg["maa"]["install_dir"]))
        user_dir = str(cfg["maa"].get("user_dir") or "").strip()
        self.maa_user_dir = Path(user_dir) if user_dir else self.run_dir / "maa-debug"
        # MaaCore 的日志会写到 user_dir\debug\asst.log（我们默认把 user_dir 设成本次运行目录），
        # 同时兼容「user_dir 指向 MAA 安装目录」的老用法：两条路径都记起始偏移，只拷新增片段。
        self.core_logs: list[tuple[Path, int]] = []
        self.task_timeout = args.task_timeout or int(cfg["run"]["task_timeout_minutes"])
        self.stall_timeout = args.stall_timeout or int(cfg["run"]["stall_timeout_minutes"])
        self.sleep_blocked = False
        self.stage_checks: list[mc.StageCheck] = []
        self.skipped_tasks: list[dict] = []
        self.notes: list[str] = []
        self.crashed = False

    # ---------------- 总入口 ----------------

    def run(self) -> int:
        self.logger.info(f"=== MAA 明日方舟 自动运行开始（run_id={self.run_id}，mode={self.mode}）===")
        try:
            if self.preflight() and not self.dry_run:
                self.execute()
        except KeyboardInterrupt:
            self.abort_reason = self.abort_reason or "收到 Ctrl+C，手工中断"
            self.logger.warn(self.abort_reason)
        except Exception as exc:  # noqa: BLE001 - 报告必须落盘
            self.crashed = True
            if not self.abort_reason:
                self.abort_reason = f"运行异常：{exc}"
            self.logger.error(f"运行异常：{exc}\n{traceback.format_exc()}")
        return self.finish()

    # ---------------- 0. 前置自检 ----------------

    def preflight(self) -> bool:
        self.logger.info("── 前置自检")
        self._discover_install_dir_if_needed()

        # 必须在 MaaCore 写日志之前记下偏移，收尾时只拷贝本次新增的部分
        self.core_logs = []
        for candidate in (self.maa_user_dir / "debug" / "asst.log", self.install_dir / "debug" / "asst.log"):
            try:
                offset = candidate.stat().st_size if candidate.exists() else 0
            except OSError:
                offset = 0
            self.core_logs.append((candidate, offset))

        ldconsole = mc.resolve_path(self.cfg, str(self.cfg["emulator"].get("ldconsole") or ""))
        adb_path = mc.resolve_path(self.cfg, str(self.cfg["maa"].get("adb_path") or ""))

        checks = {
            "MAA 安装目录": self.install_dir,
            "MaaCore.dll": self.install_dir / "MaaCore.dll",
            "resource\\tasks": self.install_dir / "resource" / "tasks",
            "Python 接口": self.install_dir / "Python" / "asst" / "asst.py",
            "雷电 ldconsole": ldconsole,
            "adb": adb_path,
        }
        for name, path in checks.items():
            exists = Path(path).exists()
            self.logger.info(f"  {'OK  ' if exists else 'MISS'} {name}: {path}")
            if not exists:
                self.preflight_problems.append(f"缺少{name}：{path}")

        if not self.preflight_problems:
            try:
                self.gui_cfg = mc.load_gui_config(self.cfg)
            except (OSError, json.JSONDecodeError) as exc:
                self.preflight_problems.append(f"读取 MAA GUI 配置失败：{exc}")

        if self.gui_cfg is not None:
            try:
                self.tasks, map_warnings, map_notes = mc.build_queue(self.cfg, self.gui_cfg)
                self.warnings.extend(map_warnings)
                self.notes.extend(map_notes)
                self.logger.info(f"  任务队列共 {len(self.tasks)} 项：")
                for index, spec in enumerate(self.tasks, 1):
                    self.logger.info(f"    {index}. {spec.describe()}")
            except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as exc:
                self.preflight_problems.append(f"任务映射失败：{exc}")
                self.logger.error(f"任务映射失败：{exc}")

        gui_rel = str(self.cfg["maa"].get("gui_config") or "")
        gui_path = Path(gui_rel)
        if not gui_path.is_absolute():
            gui_path = self.install_dir / gui_rel

        ctx_client = None
        if self.gui_cfg is not None:
            ctx_client = mc._global_context(self.gui_cfg)["client_type"]

        self.env_info = {
            "config_path": self.cfg["_config_path"],
            "maa_install_dir": str(self.install_dir),
            "maa_core_version": None,
            "resource_version": self._read_resource_version(),
            "gui_config_path": str(gui_path),
            "gui_config_name": (self.gui_cfg or {}).get("Current"),
            "client_type": ctx_client,
            "adb_path": str(adb_path),
            "adb_address": str(self.cfg["maa"]["adb_address"]),
            "touch_mode": str(self.cfg["maa"]["touch_mode"]),
            "instance_index": int(self.cfg["emulator"]["instance_index"]),
            "plaunched_by_us": False,
        }

        # 先刷新活动关卡数据，再校验关卡（两者都用 MAA 官方接口）
        if not self.preflight_problems:
            self._refresh_activity_cache()

        # 关卡有效性校验（活动结束后 YW-8 这类关卡会被明确拦下，而不是让 MAA 瞎试）
        if self.tasks and self.cfg["run"].get("verify_stage", True):
            self._verify_stages()

        if not self.preflight_problems and self.tasks:
            try:
                self.asst_cls, self.instance_option, loaded = load_maa_core(
                    self.install_dir, self.maa_user_dir, self.logger
                )
                if not loaded:
                    self.preflight_problems.append("MaaCore 资源加载失败（AsstLoadResource 返回 false）")
                else:
                    self.env_info["maa_core_version"] = self._read_core_version()
            except (ImportError, OSError, AttributeError, TypeError) as exc:
                self.preflight_problems.append(f"加载 MaaCore 失败：{exc}")
                self.logger.error(f"加载 MaaCore 失败：{exc}\n{traceback.format_exc()}")

        if not self.tasks and not self.preflight_problems:
            self.preflight_problems.append("任务队列为空（检查 MAA GUI 配置里任务是否都启用）")

        if self.preflight_problems:
            self.logger.error("环境自检未通过，跳过执行：")
            for problem in self.preflight_problems:
                self.logger.error(f"  - {problem}")
            return False

        self.logger.info("前置自检通过")
        if self.dry_run:
            self.logger.info("干跑模式：不启动模拟器、不执行任务（以上任务队列即实际会执行的内容）")
        return True

    def _discover_install_dir_if_needed(self) -> None:
        """MAA 自动更新会在同级目录里生成新版本文件夹（MAA-vX.Y.Z-win-x64）。

        配置里的 install_dir 指向旧目录时会找不到 MaaCore.dll，这里自动退化到最新的一份，
        并在报告里留下警告，提示更新 config.json。
        """
        if (self.install_dir / "MaaCore.dll").exists():
            return
        candidates: list[Path] = []
        for root in (self.install_dir, self.install_dir.parent):
            try:
                if root.is_dir():
                    candidates.extend(found.parent for found in root.glob("*/MaaCore.dll"))
            except OSError:
                continue
        if not candidates:
            return
        newest = max(candidates, key=lambda path: path.stat().st_mtime)
        self.logger.warn(
            f"配置的 MAA 目录里没有 MaaCore.dll，自动改用 {newest}"
            f"（建议把 config.json 的 maa.install_dir 更新成这个路径）"
        )
        self.install_dir = newest

    def _refresh_activity_cache(self) -> None:
        """把 MAA 官方 OTA 的活动关卡数据刷到 <MAA>\\cache\\resource\\tasks.json。

        为什么必须做：活动关卡的导航数据（YW-Open / YW-8@SideStoryStage 之类）只存在于增量资源
        `cache` 里，而只有 MAA 图形界面才会去联网更新它。如果从此只用这个脚本跑，新活动开始时
        cache 还是旧的，新关卡就导航不了（表现为 UnsupportedLevel / 关卡识别失败）。
        这里让脚本自己按 TTL 更新，失败就继续用旧数据，不影响本次运行。
        """
        if not self.cfg["run"].get("update_activity_cache", True):
            return
        target = self.install_dir / "cache" / "resource" / "tasks.json"
        ttl_hours = float(self.cfg["run"].get("activity_cache_ttl_hours") or 6)
        info: dict = {"path": str(target), "updated": False, "reason": ""}

        if target.exists() and ttl_hours > 0:
            try:
                age_hours = (time.time() - target.stat().st_mtime) / 3600
                if age_hours < ttl_hours:
                    info["reason"] = f"本地数据 {age_hours:.1f} 小时前更新过（TTL {ttl_hours:g}h），跳过"
                    self.logger.info(f"  活动关卡数据：{info['reason']}")
                    self.env_info["activity_cache"] = info
                    return
            except OSError:
                pass

        url = str(self.cfg["run"].get("activity_tasks_api") or "")
        if not url:
            return
        timeout = int(self.cfg["run"].get("stage_check_timeout_sec") or 10)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "maa-arknights-auto"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read()
            data = json.loads(payload.decode("utf-8"))
            if not isinstance(data, dict) or len(data) < 5:
                raise ValueError("返回内容不像活动关卡数据")
        except Exception as exc:  # noqa: BLE001 - 网络问题继续用旧数据
            info["reason"] = f"更新失败，继续用本地旧数据（{type(exc).__name__}: {exc}）"
            self.logger.warn(f"  活动关卡数据：{info['reason']}")
            self.env_info["activity_cache"] = info
            return

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.copy2(target, target.with_suffix(".json.bak"))
            temp = target.with_suffix(".json.tmp")
            temp.write_bytes(payload)
            temp.replace(target)
            info.update({"updated": True, "size": len(payload), "entries": len(data), "reason": "已更新"})
            self.logger.info(f"  活动关卡数据：已更新（{len(payload)} 字节 / {len(data)} 条）")
        except OSError as exc:
            info["reason"] = f"写入失败，继续用本地旧数据：{exc}"
            self.logger.warn(f"  活动关卡数据：{info['reason']}")
        self.env_info["activity_cache"] = info

    def _verify_stages(self) -> None:
        """用 MAA 官方活动接口 + 本地基础关卡表校验每个战斗任务的关卡是否还能打。

        判定依据（两层）：
          1. MAA 官方活动接口里能查到 → 看活动的到期时间，过期就判定不可用；
          2. 查不到 → 看 MAA 基础关卡表 resource/stages.json 里有没有（常规/别传关卡都在里面）。
        活动关卡（例如 YW-8）只存在于活动增量资源里，基础表里没有，所以活动一结束就会被拦下。
        """
        stages: list[str] = []
        for spec in self.tasks:
            if spec.core_type == "Fight":
                stage = str(spec.params.get("stage") or "").strip()
                if stage and stage not in stages:
                    stages.append(stage)
        if not stages:
            return

        self.logger.info("── 关卡有效性校验")
        api_url = str(self.cfg["run"].get("stage_activity_api") or "")
        timeout = int(self.cfg["run"].get("stage_check_timeout_sec") or 10)
        activity_data, api_error = mc.fetch_stage_activity(api_url, timeout)
        if activity_data is None:
            self.logger.warn(f"  活动接口查询失败（{api_error}），只能按本地基础关卡表判断")
        base_codes = mc.load_base_stage_codes(self.install_dir / "resource" / "stages.json")
        client_type = str(self.env_info.get("client_type") or "Official")

        invalid: dict[str, mc.StageCheck] = {}
        for stage in stages:
            check = mc.check_stage_availability(stage, client_type, activity_data, base_codes, api_error)
            self.stage_checks.append(check)
            self.logger.info(f"  {'OK  ' if check.ok else 'FAIL'} 关卡 {check.stage}：{check.message}")
            if not check.ok:
                invalid[check.stage] = check
        if not invalid:
            return

        mode = str(self.cfg["run"].get("on_invalid_stage") or "skip").lower()
        for check in invalid.values():
            message = f"关卡 {check.stage} 不可用：{check.message}"
            self.warnings.append(message)
            if mode == "abort":
                self.preflight_problems.append(message)

        if mode == "abort":
            self.logger.error("  按配置 on_invalid_stage=abort，中止本次运行")
            return
        if mode == "allow":
            self.logger.warn("  按配置 on_invalid_stage=allow，不拦下这些任务，交由 MAA 自己处理（可能又点又试）")
            return

        kept: list[mc.TaskSpec] = []
        for spec in self.tasks:
            stage = str(spec.params.get("stage") or "").strip() if spec.core_type == "Fight" else ""
            if stage in invalid:
                reason = invalid[stage].message
                self.skipped_tasks.append(
                    {"label": spec.label, "core_type": spec.core_type, "stage": stage, "reason": reason}
                )
                self.logger.warn(f"  已跳过任务「{spec.label}」（关卡 {stage} 不可用，改为按 on_invalid_stage={mode} 跳过）")
                continue
            kept.append(spec)
        self.tasks = kept

    def _read_resource_version(self) -> dict | None:
        try:
            return json.loads((self.install_dir / "resource" / "version.json").read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return None

    def _read_core_version(self) -> str | None:
        try:
            probe = self.asst_cls()
            try:
                return str(probe.get_version())
            finally:
                del probe
        except Exception:  # pragma: no cover
            return None

    # ---------------- 电源管理 ----------------

    def _prevent_sleep(self) -> None:
        """运行期间阻止系统自动休眠/待机。

        无人值守最容易翻车的地方：机器被唤醒跑了几分钟后因为「无用户操作」又睡回去，
        模拟器与 adb 一起断，任务半途而废。SetThreadExecutionState 只阻止「空闲自动睡眠」，
        不会阻止你手动睡眠/关机；运行结束时在 finish() 里恢复原状。
        """
        if not self.cfg["run"].get("prevent_sleep", True) or os.name != "nt":
            return
        try:
            if ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                self.sleep_blocked = True
                self.env_info["prevent_sleep"] = True
                self.logger.info("  已阻止系统在本次运行期间自动休眠（结束时恢复）")
            else:
                self.env_info["prevent_sleep"] = False
                self.logger.warn("  阻止系统休眠失败（SetThreadExecutionState 返回 0）")
        except (AttributeError, OSError) as exc:
            self.env_info["prevent_sleep"] = False
            self.logger.warn(f"  阻止系统休眠时出错：{exc}")

    def _allow_sleep(self) -> None:
        if not getattr(self, "sleep_blocked", False):
            return
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
            self.logger.info("  已恢复系统电源策略（允许再次自动休眠）")
        except (AttributeError, OSError):
            pass
        finally:
            self.sleep_blocked = False

    # ---------------- 1~3. 模拟器 + MAA 执行 ----------------

    def execute(self) -> None:
        self._prevent_sleep()
        self.emulator = Emulator(self.cfg, self.logger)
        if self.args.no_launch:
            self.emulator.auto_launch = False
        if self.args.keep_emulator:
            self.emulator.quit_after_run = False

        self.logger.info("── 准备模拟器")
        if not self.emulator.ensure_ready():
            self.preflight_problems.append("模拟器未就绪（启动或 adb 连接失败）")
            return

        self.env_info["adb_address"] = self.emulator.used_address
        self.env_info["emulator_was_running"] = self.emulator.was_running
        self.env_info["emulator_launched_by_us"] = self.emulator.launched_by_us

        if self.cfg["run"].get("verify_game_package"):
            package = str(self.cfg["run"].get("game_package") or "")
            if package:
                if self.emulator.package_installed(package):
                    self.logger.info(f"  模拟器中已安装 {package}")
                else:
                    message = f"模拟器里没找到客户端 {package}，MAA 的「开始唤醒」可能失败"
                    self.warnings.append(message)
                    self.logger.warn(f"  {message}")

        self.logger.info("── 启动 MAA 任务")
        self.recorder = RunRecorder(
            self.logger,
            self.run_dir / "callbacks.jsonl",
            int(self.cfg["maa"].get("report_http_timeout_sec") or 15),
        )
        callback = make_callback(self.asst_cls, self.recorder)
        asst = self.asst_cls(callback=callback)
        try:
            self._drive(asst)
        finally:
            try:
                asst.stop()
            except Exception:  # pragma: no cover
                pass
            time.sleep(1.0)
            try:
                del asst
            except Exception:  # pragma: no cover
                pass
            gc.collect()

    def _drive(self, asst) -> None:
        try:
            asst.set_instance_option(self.instance_option.touch_type, str(self.cfg["maa"]["touch_mode"]))
        except Exception as exc:  # pragma: no cover
            self.logger.warn(f"设置触控模式失败：{exc}")

        attempts = max(1, int(self.cfg["run"]["connect_retry"]))
        connected = False
        for attempt in range(1, attempts + 1):
            self.logger.info(f"  连接设备 {self.emulator.used_address}（第 {attempt}/{attempts} 次）")
            if asst.connect(
                str(mc.resolve_path(self.cfg, str(self.cfg["maa"]["adb_path"]))),
                self.emulator.used_address,
                str(self.cfg["maa"]["adb_config"]),
            ):
                connected = True
                break
            time.sleep(int(self.cfg["run"]["connect_retry_interval_sec"]))
        if not connected:
            self.abort_reason = "MAA 连接设备失败"
            self.logger.error(f"  {self.abort_reason}")
            return
        self.logger.info("  设备连接成功")

        appended = 0
        for spec in self.tasks:
            params = {k: v for k, v in spec.params.items() if not k.startswith("_")}
            task_id = asst.append_task(spec.core_type, params)
            if not task_id:
                message = f"任务「{spec.label}」提交失败（MaaCore 返回 0）"
                self.warnings.append(message)
                self.logger.warn(f"  {message}")
                continue
            task_id = int(task_id)
            self.recorder.taskid_to_label[task_id] = spec.label
            appended += 1
            self.task_entries.append(
                {
                    "task_id": task_id,
                    "label": spec.label,
                    "core_type": spec.core_type,
                    "params": spec.params,
                    "gui_type": spec.gui_type,
                    "status": "not-run",
                    "started_at": None,
                    "finished_at": None,
                    "duration_sec": None,
                    "error": None,
                }
            )
            self.logger.info(f"  已提交任务 {spec.label}（id={task_id}）")

        if not appended:
            self.abort_reason = "没有任何任务提交成功"
            self.logger.error(f"  {self.abort_reason}")
            return

        asst.start()
        self.logger.info("  任务已启动，等待执行结束 …")

        last_progress = time.monotonic()
        while asst.running():
            time.sleep(1.0)
            now = time.monotonic()
            idle = now - self.recorder.last_event_ts
            if idle > self.stall_timeout * 60:
                self.abort_reason = f"MAA 已 {int(idle // 60)} 分钟没有任何回调，判定卡死并停止"
                self.logger.error(f"  {self.abort_reason}")
                asst.stop()
                break
            if now - self.t0 > self.task_timeout * 60:
                self.abort_reason = f"单次运行超过 {self.task_timeout} 分钟，强制停止"
                self.logger.error(f"  {self.abort_reason}")
                asst.stop()
                break
            if now - last_progress > 600:
                last_progress = now
                self.logger.info(f"  …仍在执行，已运行 {format_duration(now - self.t0)}")

    # ---------------- 4~5. 收尾与报告 ----------------

    def finish(self) -> int:
        self.logger.info("── 收尾")
        self._allow_sleep()
        if self.emulator is not None:
            try:
                self.emulator.cleanup(keep=self.args.keep_emulator)
            except Exception as exc:  # pragma: no cover
                self.logger.warn(f"退出模拟器时出错：{exc}")

        if self.cfg["report"].get("copy_maa_debug_log"):
            chunks: list[bytes] = []
            for path, offset in self.core_logs:
                try:
                    if not path.exists() or path.stat().st_size <= offset:
                        continue
                    with path.open("rb") as handle:
                        handle.seek(offset)
                        chunks.append(handle.read())
                except OSError:
                    continue
            if chunks:
                target = self.run_dir / "maa-core-tail.log"
                target.write_bytes(b"\n".join(chunks))
                self.logger.info(f"  已保存 MAA 核心日志（{sum(len(c) for c in chunks)} 字节）")
            else:
                self.logger.info("  MAA 核心日志本次没有新增内容")

        snapshot = empty_snapshot()
        if self.recorder is not None:
            snapshot = self.recorder.snapshot()
            self.recorder.close()

        self._apply_chain_status(snapshot)
        result = self._classify(snapshot)
        exit_code = _RESULT_EXIT.get(result, EXIT_FAILED)

        report = self._compose_report(result, exit_code, snapshot)
        for warn in report.get("stage_usage", {}).get("warnings", []):
            self.logger.warn(f"  {warn}")
        (self.run_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        summary = build_summary_text(report)
        (self.run_dir / "summary.txt").write_text(summary, encoding="utf-8")
        append_run_index(self.report_dir, report)
        self._build_dashboard()

        self.logger.info("")
        self.logger.info(summary)
        self.logger.info(
            f"运行结束：{RESULT_TEXT.get(result, result)}（exit={exit_code}），报告目录 {self.run_dir}"
        )
        self.logger.close()
        return exit_code

    def _build_dashboard(self) -> None:
        """运行结束后重建可视化看板。

        任何失败都只记一条警告，绝不影响本次运行的结论与退出码。
        """
        if not (self.cfg.get("dashboard") or {}).get("enabled", True):
            return
        builder = HERE / "dashboard" / "build_dashboard.py"
        if not builder.exists():
            return
        try:
            code, out = run_capture(
                [sys.executable, str(builder), "--config", self.cfg["_config_path"], "--quiet"],
                timeout=180,
            )
            if code == 0:
                self.logger.info("  看板已更新：dashboard\\index.html")
            else:
                self.logger.warn(f"  看板重建失败（exit={code}）：{out.strip()[:300]}")
        except Exception as exc:  # noqa: BLE001 - 看板不能拖垮运行
            self.logger.warn(f"  看板重建失败（不影响本次运行）：{exc}")

    def _classify(self, snapshot: dict) -> str:
        if self.preflight_problems:
            return "env-error"
        if self.crashed:
            return "unexpected"
        if self.dry_run:
            return "dry-run"
        if self.abort_reason:
            return "timeout" if "超过" in self.abort_reason or "卡死" in self.abort_reason else "aborted"
        if not self.task_entries:
            return "failed"

        # 以任务链的实际状态为准：全部完成 = 成功；完成一部分 = 部分完成；一个都没完成 = 失败。
        # 致命的连接/核心错误只用来把「全完成」下调为「部分完成」，可恢复的子任务报错（进 notices）
        # 不影响结论，否则像「信用商店信用不足」这种正常情况会把整次运行判成失败。
        statuses = [entry["status"] for entry in self.task_entries]
        completed = statuses.count("completed")
        fatal = bool(snapshot.get("errors"))

        if completed == len(statuses):
            # 有任务因为关卡失效被跳过时，不能算「成功」——得让人一眼看到红色
            return "partial" if (fatal or self.skipped_tasks) else "success"
        if completed == 0:
            return "failed"
        return "partial"

    def _apply_chain_status(self, snapshot: dict) -> None:
        by_id = {chain["task_id"]: chain for chain in snapshot.get("chains", [])}
        for entry in self.task_entries:
            chain = by_id.get(entry["task_id"])
            if not chain:
                continue
            status = chain.get("status")
            if status in {"completed", "error", "stopped", "running"}:
                entry["status"] = status
            entry["started_at"] = chain.get("started_at") or entry["started_at"]
            entry["finished_at"] = chain.get("finished_at") or entry["finished_at"]
            entry["duration_sec"] = chain.get("duration_sec") or entry["duration_sec"]
            entry["error"] = chain.get("error") or entry["error"]

        if snapshot.get("all_completed"):
            for entry in self.task_entries:
                if entry["status"] == "not-run":
                    entry["status"] = "unknown"

    def _compose_report(self, result: str, exit_code: int, snapshot: dict) -> dict:
        artifacts = {
            "日志": str(self.run_dir / "run.log"),
            "报告 JSON": str(self.run_dir / "report.json"),
            "文本摘要": str(self.run_dir / "summary.txt"),
            "原始回调": str(self.run_dir / "callbacks.jsonl"),
        }
        core_tail = self.run_dir / "maa-core-tail.log"
        if core_tail.exists():
            artifacts["MAA 核心日志片段"] = str(core_tail)
        if (self.run_dir / "maa-debug").exists():
            artifacts["MAA 调试数据"] = str(self.run_dir / "maa-debug")

        tasks = self.task_entries
        battles_by_task = snapshot.get("battles_by_task") or {}
        for entry in tasks:
            entry["battles"] = int(battles_by_task.get(entry.get("task_id"), 0))
        if self.dry_run:
            stage_usage = {"items": [], "warnings": [], "notes": []}
        else:
            stage_usage = analyze_stage_usage(tasks, snapshot.get("drop_events") or [])
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "result": result,
            "exit_code": exit_code,
            "started_at": self.started.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "duration_sec": round(time.monotonic() - self.t0, 1),
            "run_dir": str(self.run_dir),
            "environment": self.env_info,
            "preflight": {"problems": self.preflight_problems, "passed": not self.preflight_problems},
            "planned_tasks": [spec.describe() for spec in self.tasks],
            "stage_checks": [check.to_json() for check in self.stage_checks],
            "skipped_tasks": self.skipped_tasks,
            "tasks": tasks,
            "summary": {
                "total": len(tasks),
                "completed": sum(1 for t in tasks if t["status"] == "completed"),
                "failed": sum(1 for t in tasks if t["status"] == "error"),
                "stopped": sum(1 for t in tasks if t["status"] == "stopped"),
                "not_run": sum(1 for t in tasks if t["status"] == "not-run"),
                "all_tasks_completed_callback": bool(snapshot.get("all_completed")),
            },
            "counters": snapshot.get("counters", {}),
            "drops": snapshot.get("drops", {}),
            "drop_events": snapshot.get("drop_events", []),
            "recruit": snapshot.get("recruit", {}),
            "infrast": snapshot.get("infrast", {}),
            "connection_events": snapshot.get("connection_events", []),
            "uploads": snapshot.get("uploads", {}),
            "errors": snapshot.get("errors", []),
            "notices": snapshot.get("notices", []),
            "stage_usage": stage_usage,
            "notes": list(self.notes) + list(snapshot.get("notes", [])) + stage_usage["notes"],
            "warnings": self.warnings + stage_usage["warnings"],
            "abort_reason": self.abort_reason,
            "artifacts": artifacts,
        }


# ======================================================================================
# 关卡核对：配置要打的关卡 vs 实际打到的关卡
# ======================================================================================

STAGE_USAGE_TEXT = {
    "match": "一致",
    "mismatch": "不一致",
    "empty-expected": "未指定",
    "no-battle": "没打成",
    "unknown-stage": "无法核对",
}


def _render_stages(counter: dict[str, int]) -> str:
    return "、".join(
        f"{stage}×{count}" for stage, count in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def analyze_stage_usage(tasks: list[dict], drop_events: list[dict]) -> dict:
    """对比「配置的关卡」与「实际打到的关卡」。

    脚本只校验得了「关卡名在 MAA 数据里是否有效」（活动是否过期之类），
    但 MAA 真正进哪一关是运行期才决定的：关卡留空时它会点「前往上一次作战」，
    导航失败时也可能沿用它。只有 StageDrops 回调里的 ``stageCode`` 才是事实，
    所以这里逐任务核对，并把差异写进警告——不然「配置 A、实际打 B」会静默发生。
    """
    task_ids = {task.get("task_id") for task in tasks if isinstance(task.get("task_id"), int)}
    by_task: dict[int, dict[str, int]] = {}
    orphan: dict[str, int] = {}
    for event in drop_events:
        if not isinstance(event, dict):
            continue
        stage = str(event.get("stage") or "?")
        task_id = event.get("taskid")
        if isinstance(task_id, int) and task_id in task_ids:
            bucket = by_task.setdefault(task_id, {})
        else:
            bucket = orphan
        bucket[stage] = bucket.get(stage, 0) + 1

    items: list[dict] = []
    warnings: list[str] = []
    notes: list[str] = []

    for task in tasks:
        if str(task.get("core_type")) != "Fight":
            continue
        task_id = task.get("task_id")
        label = str(task.get("label") or "理智作战")
        expected = str((task.get("params") or {}).get("stage") or "").strip()
        actual = dict(by_task.get(task_id, {}))
        battles = int(task.get("battles") or 0)
        drop_events = sum(actual.values())
        item = {
            "task_id": task_id,
            "label": label,
            "expected": expected,
            "actual": actual,
            "battles": battles,
            "drop_events": drop_events,
        }

        if not expected:
            item["status"] = "empty-expected"
            if battles:
                seen = _render_stages(actual) if actual else "（没识别到掉落结算，无法确认是哪一关）"
                notes.append(
                    f"任务「{label}」没有指定关卡，打的是「上一次作战」：{seen}（共 {battles} 场）"
                )
            else:
                notes.append(f"任务「{label}」没有指定关卡，本次也没有作战")
        elif battles == 0:
            item["status"] = "no-battle"
            warnings.append(
                f"任务「{label}」配置关卡 {expected}，但一场都没打成："
                "MAA 可能没能进入该关卡（关卡名已变 / 导航失败），这一项等于没打"
            )
        elif not drop_events:
            item["status"] = "unknown-stage"
            notes.append(
                f"任务「{label}」配置关卡 {expected}，打了 {battles} 场，"
                "但没识别到掉落结算，无法核对实际打的是哪一关"
            )
        else:
            mismatch = {
                stage: count for stage, count in actual.items() if stage.upper() != expected.upper()
            }
            if mismatch:
                item["status"] = "mismatch"
                warnings.append(
                    f"任务「{label}」配置关卡 {expected}，实际却打到 {_render_stages(mismatch)}"
                    f"（共 {battles} 场，识别到 {drop_events} 次掉落结算）："
                    "MAA 没进到配置的关卡（常见原因是关卡名对不上、或导航失败后沿用「上一次作战」）"
                )
            else:
                item["status"] = "match"
        items.append(item)

    if orphan:
        notes.append(
            f"另有 {sum(orphan.values())} 场作战无法对应到具体任务：{_render_stages(orphan)}"
        )
    return {"items": items, "warnings": warnings, "notes": notes}


# ======================================================================================
# 报告文本
# ======================================================================================


def build_summary_text(report: dict) -> str:
    env = report["environment"]
    lines: list[str] = []
    add = lines.append
    add("=" * 70)
    add("MAA 明日方舟 自动运行报告")
    add("=" * 70)
    add(f"运行编号 : {report['run_id']}")
    add(f"触发方式 : {report['mode']}")
    add(f"运行结果 : {RESULT_TEXT.get(report['result'], report['result'])}（{report['result']}）")
    add(f"开始时间 : {report['started_at']}")
    add(f"结束时间 : {report['finished_at']}   耗时 {format_duration(report['duration_sec'])}")
    launched = "本次启动" if env.get("emulator_launched_by_us") else (
        "原本就在运行" if env.get("emulator_was_running") else "未启动"
    )
    add(f"模拟器   : 雷电实例 {env.get('instance_index')}，adb {env.get('adb_address')}（{launched}）")
    add(
        f"MAA      : MaaCore {env.get('maa_core_version') or '未知'}，"
        f"配置 {env.get('gui_config_name')}，客户端 {env.get('client_type')}"
    )
    if report.get("abort_reason"):
        add(f"中断原因 : {report['abort_reason']}")
    add("")

    add("── 任务明细 " + "─" * 55)
    if not report["tasks"] and report["mode"] != "dry-run":
        add("  （没有实际执行的任务）")
    status_text = {
        "completed": "完成",
        "error": "失败",
        "stopped": "已停止",
        "running": "未结束",
        "not-run": "未执行",
        "unknown": "未知",
    }
    for index, task in enumerate(report["tasks"], 1):
        params = {k: v for k, v in task["params"].items() if not k.startswith("_")}
        duration = format_duration(task.get("duration_sec")) if task.get("duration_sec") else "-"
        add(
            f"  {index}. {task['label']:<8} {task['core_type']:<10}"
            f" {status_text.get(task['status'], task['status']):<4} {duration:>10}   "
            f"{json.dumps(params, ensure_ascii=False)}"
        )
        if task.get("error"):
            add(f"        错误：{task['error']}")
    if report["mode"] == "dry-run" and report.get("planned_tasks"):
        add("  （干跑：以上为计划执行的任务序列，未真正提交）")
    add("")

    if report.get("stage_checks"):
        add("── 关卡有效性校验 " + "─" * 50)
        for check in report["stage_checks"]:
            add(f"  {'可用' if check['ok'] else '不可用'}  关卡 {check['stage']}：{check['message']}")
        for skipped in report.get("skipped_tasks") or []:
            add(f"  已跳过  任务「{skipped['label']}」（关卡 {skipped['stage']} 不可用）")
        add("")

    usage_items = (report.get("stage_usage") or {}).get("items") or []
    if usage_items:
        add("── 关卡使用（配置 vs 实际）" + "─" * 40)
        for item in usage_items:
            expected = item.get("expected") or "（未指定，打上一次作战）"
            actual = item.get("actual") or {}
            battles = int(item.get("battles") or 0)
            seen = _render_stages(actual) if actual else ("没识别到掉落结算" if battles else "无作战")
            mark = STAGE_USAGE_TEXT.get(str(item.get("status")), str(item.get("status")))
            add(
                f"  [{mark}] {item.get('label', '理智作战'):<8}"
                f" 配置 {expected} → 实际 {seen}（{battles} 场）"
            )
        add("")

    add("── 掉落统计 " + "─" * 55)
    if not report["drops"]:
        add("  （本次没有识别到掉落结算）")
    for stage, items in report["drops"].items():
        add(f"  [{stage}]")
        for name, info in sorted(items.items(), key=lambda kv: -(kv[1].get("added") or 0)):
            added = info.get("added") or 0
            add(f"    {name:<12} 累计 {info.get('total')}" + (f"（本次 +{added}）" if added else ""))
    add("")

    recruit = report.get("recruit") or {}
    add("── 公开招募 " + "─" * 55)
    add(
        f"  识别 {len(recruit.get('results', []))} 次，刷新 {recruit.get('refreshed', 0)}，"
        f"无招聘许可 {recruit.get('no_permit', 0)}，错误 {recruit.get('errors', 0)}"
    )
    if recruit.get("special_tags"):
        add(f"  特殊 Tag：{', '.join(recruit['special_tags'])}")
    if recruit.get("preserved_tags"):
        add(f"  保留 Tag：{', '.join(recruit['preserved_tags'])}")
    for item in recruit.get("results", [])[:20]:
        add(f"  · 最高 {item.get('level')}★  tags={item.get('tags')}")
    add("")

    counters = report.get("counters") or {}
    add("── 资源消耗 " + "─" * 55)
    add(
        f"  作战次数 {counters.get('battles', 0)}，理智药 {counters.get('medicine_used', 0)}"
        f"（含 48h 过期 {counters.get('expiring_medicine_used', 0)}），碎石 {counters.get('stone_used', 0)}"
    )
    add(f"  公招确认 {counters.get('recruit_confirmed', 0)}，加急 {counters.get('expedite_used', 0)}")
    infrast = report.get("infrast") or {}
    if infrast.get("facilities"):
        add(f"  基建进入设施：{json.dumps(infrast['facilities'], ensure_ascii=False)}")
    if infrast.get("products"):
        add(f"  基建产物：{json.dumps(infrast['products'], ensure_ascii=False)}")
    add("")

    uploads = report.get("uploads") or {}
    if uploads.get("total"):
        add(f"── 第三方上报 共 {uploads['total']} 次，成功 {uploads.get('ok', 0)}，失败 {uploads.get('failed', 0)}")
        add("")

    if report["warnings"]:
        add("── 警告 " + "─" * 59)
        for warn in report["warnings"]:
            add(f"  ! {warn}")
        add("")

    if report["notes"] or report.get("notices"):
        add("── 提示（可恢复问题，不影响运行结论）" + "─" * 32)
        for note in report["notes"]:
            add(f"  · {note}")
        for notice in report.get("notices", []):
            add(f"  · [{notice['kind']}] {notice['message']}")
        add("")

    if report["errors"]:
        add("── 错误（影响运行结论）" + "─" * 46)
        for err in report["errors"]:
            add(f"  x [{err['kind']}] {err['message']}")
        add("")

    add("── 产物 " + "─" * 59)
    for key, value in report["artifacts"].items():
        if value:
            add(f"  {key}: {value}")
    add("=" * 70)
    return "\n".join(lines)


def cleanup_old_runs(log_root: Path, keep_days: int, logger: Logger) -> None:
    if keep_days <= 0 or not log_root.is_dir():
        return
    cutoff = time.time() - keep_days * 86400
    for child in log_root.iterdir():
        if not child.is_dir() or not re.match(r"^\d{4}-\d{2}-\d{2}_\d{6}", child.name):
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                logger.info(f"清理过期运行目录：{child.name}")
        except OSError:
            pass


def append_run_index(log_root: Path, report: dict) -> None:
    counters = report.get("counters") or {}
    entry = {
        "run_id": report["run_id"],
        "mode": report["mode"],
        "started_at": report["started_at"],
        "finished_at": report["finished_at"],
        "duration_sec": report["duration_sec"],
        "result": report["result"],
        "battles": counters.get("battles", 0),
        "medicine_used": counters.get("medicine_used", 0),
        "stone_used": counters.get("stone_used", 0),
        "tasks_completed": report["summary"]["completed"],
        "tasks_total": report["summary"]["total"],
        "errors": len(report["errors"]),
        "notices": len(report.get("notices") or []),
        "dir": report["run_dir"],
    }
    with (log_root / "runs.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ======================================================================================
# CLI
# ======================================================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MAA + 雷电模拟器 明日方舟 定时自动运行器")
    parser.add_argument("--config", default=str(HERE / "config.json"), help="配置文件路径")
    parser.add_argument(
        "--mode", default="manual", choices=["manual", "scheduled", "dry-run"], help="触发方式（写入报告）"
    )
    parser.add_argument("--dry-run", action="store_true", help="只做自检与任务映射，不启动模拟器、不执行任务")
    parser.add_argument("--no-launch", action="store_true", help="不启动模拟器（要求它已经在运行）")
    parser.add_argument("--keep-emulator", action="store_true", help="跑完不退出模拟器")
    parser.add_argument("--task-timeout", type=int, default=None, help="覆盖总超时（分钟）")
    parser.add_argument("--stall-timeout", type=int, default=None, help="覆盖卡死判定（分钟，无任何回调）")
    parser.add_argument(
        "--ignore-lock",
        action="store_true",
        help="忽略单实例锁（仅在你确知另一个运行已卡死时使用）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = mc.load_config(Path(args.config))
    except (OSError, json.JSONDecodeError) as exc:
        if sys.stderr is not None:
            print(f"读取配置失败：{exc}", file=sys.stderr)
        return EXIT_ENV_ERROR
    lock = RunLock(cfg, args)
    if not lock.acquire():
        return EXIT_LOCKED
    try:
        runner = Runner(args, cfg)
    except Exception as exc:  # noqa: BLE001
        lock.release()
        if sys.stderr is not None:
            print(f"初始化运行器失败：{exc}", file=sys.stderr)
        return EXIT_UNEXPECTED
    try:
        return runner.run()
    except Exception as exc:  # noqa: BLE001 - 最后一道防线
        runner.logger.error(f"运行器崩溃：{exc}\n{traceback.format_exc()}")
        runner.logger.close()
        return EXIT_UNEXPECTED
    finally:
        lock.release()


if __name__ == "__main__":
    code = main()
    # MaaCore 是原生库，解释器收尾阶段卸载它有可能直接崩掉、把真实退出码覆盖成 1
    # （实测出现过：报告里写的是 exit=3，进程实际退出码却是 1）。
    # 报告与日志都已落盘，这里直接硬退出，保证计划任务能拿到真实退出码。
    try:
        if sys.stdout is not None:
            sys.stdout.flush()
        if sys.stderr is not None:
            sys.stderr.flush()
    except (OSError, ValueError):
        pass
    os._exit(code)
