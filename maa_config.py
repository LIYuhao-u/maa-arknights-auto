# -*- coding: utf-8 -*-
# 作者: leo <1193725859@qq.com> · 创建 2026-10-12 · 最近修改 2026-10-17
# 项目: maa-arknights-auto（MIT License） · 见 README.md / AUTHORS.md

"""配置加载 + MAA GUI 任务队列 -> MaaCore 参数映射。

这个模块只做两件事：
1. 读 config.json（带默认值深度合并，忽略以 _ 开头的注释键）；
2. 把 MAA GUI 的 gui.new.json 里的任务队列，翻译成 MaaCore ``AsstAppendTask`` 需要的
   ``(type, params)``，这样脚本跑起来的行为和你在 MAA 界面里点「开始唤醒 / Link Start」一致。

凡是映射函数没有消费掉的 GUI 字段，都会被记进 ``TaskSpec.unmapped``，最终写进运行报告，
避免配置被静默忽略。
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------------------
# 默认配置（config.json 缺失的键用这里的值）
# --------------------------------------------------------------------------------------

DEFAULTS: dict[str, Any] = {
    "maa": {
        "install_dir": "",
        "gui_config": "config\\gui.new.json",
        "adb_path": "",
        "adb_address": "emulator-5554",
        "adb_config": "LDPlayer",
        "touch_mode": "minitouch",
        "user_dir": "",
        "report_to_penguin": True,
        "report_to_yituliu": True,
        "report_http_timeout_sec": 15,
    },
    "emulator": {
        "ldconsole": "",
        "instance_index": 0,
        "auto_launch": True,
        "launch_wait_sec": 150,
        "launch_retry": 3,
        "launch_retry_interval_sec": 20,
        "boot_settle_sec": 20,
        "quit_after_run": True,
        "kill_residual_processes": True,
        "residual_process_names": ["dnplayer.exe", "ldplayerhelper.exe"],
    },
    "run": {
        "python_path": "",
        "task_timeout_minutes": 300,
        "stall_timeout_minutes": 25,
        "connect_retry": 3,
        "connect_retry_interval_sec": 15,
        "verify_game_package": True,
        "game_package": "com.hypergryph.arknights",
        "prevent_sleep": True,
        "verify_stage": True,
        "stage_activity_api": "https://api.maa.plus/MaaAssistantArknights/api/gui/StageActivityV2.json",
        "stage_check_timeout_sec": 10,
        "on_invalid_stage": "skip",
        "update_activity_cache": True,
        "activity_tasks_api": "https://api.maa.plus/MaaAssistantArknights/api/resource/tasks.json",
        "activity_cache_ttl_hours": 6,
    },
    "tasks": {
        "source": "gui",
        "override_file": "",
        "stage_overrides": {},
        "skip_tasks": [],
    },
    "report": {
        "dir": "logs",
        "keep_days": 60,
        "copy_maa_debug_log": True,
    },
    "dashboard": {
        "enabled": True,
        "dir": "dashboard",
        "include_dry_run_by_default": False,
        "max_runs": 500,
    },
    "schedule": {
        "task_name": "MAA-Arknights-Auto",
        "times": ["04:10", "12:10", "20:10"],
        "wake_to_run": True,
        "start_when_available": True,
        "execution_time_limit_hours": 6,
        "random_delay_minutes": 0,
    },
}


def _strip_comments(value: Any) -> Any:
    """递归去掉以 _ 开头的键（config.json 里用它们写注释）。"""
    if isinstance(value, dict):
        return {k: _strip_comments(v) for k, v in value.items() if not str(k).startswith("_")}
    if isinstance(value, list):
        return [_strip_comments(v) for v in value]
    return value


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(config_path: Path) -> dict:
    """读取 config.json 并与默认值合并。"""
    config_path = Path(config_path)
    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    cfg = _deep_merge(DEFAULTS, _strip_comments(raw))
    cfg["_config_dir"] = str(config_path.resolve().parent)
    cfg["_config_path"] = str(config_path.resolve())
    return cfg


def resolve_path(cfg: dict, value: str) -> Path:
    """把配置里的路径转成绝对路径（相对路径按配置文件所在目录解析）。"""
    path = Path(value)
    if not path.is_absolute():
        path = Path(cfg["_config_dir"]) / path
    return path


# --------------------------------------------------------------------------------------
# 任务映射
# --------------------------------------------------------------------------------------


@dataclass
class TaskSpec:
    """一个待提交给 MaaCore 的任务。"""

    core_type: str
    params: dict
    gui_name: str = ""
    gui_type: str = ""
    unmapped: list[str] = field(default_factory=list)
    enabled: bool = True

    @property
    def label(self) -> str:
        return self.gui_name or self.core_type

    def describe(self) -> str:
        return f"{self.label} ({self.core_type}) " + json.dumps(self.params, ensure_ascii=False)


# 每个任务里已知但不需要映射到 Core 的字段（任务元数据 / 由 GUI 自己维护的状态）
_META_KEYS = {"$type", "Name", "IsEnable", "TaskType"}

# GUI 客户端类型 -> Core 的 server 参数
_SERVER_BY_CLIENT = {
    "Official": "CN",
    "Bilibili": "CN",
    "txwy": "CN",
    "YoStarEN": "US",
    "YoStarJP": "JP",
    "YoStarKR": "KR",
}


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _split_items(text: Any) -> list[str]:
    """把 GUI 里用 ; ；, ， 分隔的一行文本拆成列表。"""
    if not text:
        return []
    return [item.strip() for item in re.split(r"[;；,，\n]", str(text)) if item.strip()]


def split_list_field(text: Any) -> list[str]:
    return _split_items(text)


def _consume(task: dict, spec: TaskSpec, *keys: str) -> None:
    """标记这些 GUI 字段已被消费（不消费的字段会进 unmapped 报告）。"""
    spec.unmapped = [k for k in spec.unmapped if k not in keys]


def _new_spec(task: dict, core_type: str) -> TaskSpec:
    spec = TaskSpec(
        core_type=core_type,
        params={},
        gui_name=str(task.get("Name") or ""),
        gui_type=str(task.get("$type") or task.get("TaskType") or ""),
        unmapped=[k for k in task.keys() if k not in _META_KEYS],
    )
    return spec


def _global_context(gui_cfg: dict) -> dict:
    """GUI 全局设置里影响任务参数的项。"""
    current = gui_cfg.get("Current") or "Default"
    configs = gui_cfg.get("Configurations") or {}
    conf = configs.get(current) or {}
    gui = conf.get("Gui") or {}
    runtime = gui.get("RuntimeSettings") or {}
    third_party = gui.get("ThirdParty") or {}
    client_type = str(runtime.get("ClientType") or "Official")
    return {
        "client_type": client_type,
        "server": _SERVER_BY_CLIENT.get(client_type, "CN"),
        "start_game": _as_bool(runtime.get("StartGame"), True),
        "report_to_penguin": _as_bool(third_party.get("ReportToPenguin")),
        "penguin_id": str(third_party.get("PenguinId") or ""),
        "report_to_yituliu": _as_bool(third_party.get("ReportToYituliu")),
        "yituliu_id": str(third_party.get("YituliuOpenApiToken") or ""),
        "config_name": current,
    }


def _map_startup(task: dict, ctx: dict) -> TaskSpec:
    spec = _new_spec(task, "StartUp")
    spec.params = {
        "client_type": ctx["client_type"],
        "start_game_enabled": ctx["start_game"],
    }
    account_switch = _as_bool(task.get("AccountSwitchEnabled"))
    if account_switch and task.get("AccountName"):
        spec.params["account_name"] = str(task.get("AccountName"))
    _consume(task, spec, "AccountName", "AccountSwitchEnabled")
    return spec


def _map_recruit(task: dict, ctx: dict) -> TaskSpec:
    """公开招募。

    按 MAA GUI 源码的推导规则（RecruitSettingsUserControlModel.Serialize）：
      * 3 星只进 confirm，不进 select；
      * 4/5/6 星同时进 select 与 confirm，先 select 后 confirm，所以数组都是升序；
      * 没有硬编码默认值：四个开关全关时两个数组都是空的。
    首选 Tags 走 first_tags（PreferTagEnabled 关掉就发空数组），保留 Tags 走 preserve_tags
    （恒定下发，未启用时发空数组），招募时限走 recruitment_time。
    """
    spec = _new_spec(task, "Recruit")

    select: list[int] = []
    confirm: list[int] = []
    if _as_bool(task.get("Level3Choose")):
        confirm.append(3)
    for level, key in ((4, "Level4Choose"), (5, "Level5Choose"), (6, "Level6Choose")):
        if _as_bool(task.get(key)):
            select.append(level)
            confirm.append(level)

    first_tags: list[str] = []
    if _as_bool(task.get("PreferTagEnabled")):
        first_tags = [str(t) for t in _as_list(task.get("Level3PreferTags")) if str(t).strip()]

    preserve_tags: list[str] = []
    if _as_bool(task.get("PreserveTagEnabled")):
        preserve_tags = [str(t) for t in _as_list(task.get("PreserveTagList")) if str(t).strip()]

    spec.params = {
        "refresh": _as_bool(task.get("RefreshLevel3")),
        "select": select,
        "confirm": confirm,
        "times": _as_int(task.get("MaxTimes"), 0),
        "set_time": True,
        "expedite": _as_bool(task.get("Expedite")),
        "first_tags": first_tags,
        "preserve_tags": preserve_tags,
        "extra_tags_mode": _as_int(task.get("ExtraTagMode"), 0),
        "recruitment_time": {
            "3": _as_int(task.get("Level3Time"), 540),
            "4": _as_int(task.get("Level4Time"), 540),
            "5": _as_int(task.get("Level5Time"), 540),
            "6": _as_int(task.get("Level6Time"), 540),
        },
        "report_to_penguin": ctx["report_to_penguin"],
        "server": ctx["server"],
    }
    # 注意：MaaCore 6.11 起 skip_robot 已废弃，等价行为改由 preserve_tags 承担，因此不再发送 skip_robot
    if _as_bool(task.get("ForceRefresh")):
        spec.params["force_refresh"] = True
    if _as_bool(task.get("Expedite")) and task.get("ExpediteTimes") is not None:
        spec.params["expedite_times"] = _as_int(task.get("ExpediteTimes"), 0)
    if ctx["penguin_id"]:
        spec.params["penguin_id"] = ctx["penguin_id"]
    if ctx["report_to_yituliu"]:
        spec.params["report_to_yituliu"] = True
        if ctx["yituliu_id"]:
            spec.params["yituliu_id"] = ctx["yituliu_id"]
    if _as_bool(task.get("Level3RecruitmentPermitReserveEnabled")):
        spec.params["level3_recruitment_permit_reserve"] = _as_int(task.get("Level3RecruitmentPermitReserve"), 0)

    _consume(
        task,
        spec,
        "Level3Choose",
        "Level4Choose",
        "Level5Choose",
        "Level6Choose",
        "RefreshLevel3",
        "ForceRefresh",
        "MaxTimes",
        "ExtraTagMode",
        "Level3Time",
        "Level4Time",
        "Level5Time",
        "Level6Time",
        "PreferTagEnabled",
        "Level3PreferTags",
        "PreserveTagEnabled",
        "PreserveTagList",
        "Expedite",
        "ExpediteTimes",
        "Level3RecruitmentPermitReserveEnabled",
        "Level3RecruitmentPermitReserve",
    )
    return spec


def _map_infrast(task: dict, ctx: dict) -> TaskSpec:
    spec = _new_spec(task, "Infrast")

    mode_text = str(task.get("Mode") or "Default")
    mode = {"Default": 0, "Custom": 10000, "Rotation": 20000}.get(mode_text, _as_int(mode_text, 0))

    rooms: list[str] = []
    for item in _as_list(task.get("RoomList")):
        if isinstance(item, dict) and item.get("Room"):
            rooms.append(str(item["Room"]))

    threshold_raw = task.get("DormThreshold", 0.3)
    try:
        threshold = float(threshold_raw)
    except (TypeError, ValueError):
        threshold = 0.3
    if threshold > 1:  # GUI 存的是百分数，例如 30 表示 30%
        threshold = threshold / 100.0

    spec.params = {
        "mode": mode,
        "facility": rooms,
        "drones": str(task.get("UsesOfDrones") or "_NotUse"),
        "threshold": threshold,
        "replenish": _as_bool(task.get("OriginiumShardAutoReplenishment")),
        "dorm_notstationed_enabled": _as_bool(task.get("DormFilterNotStationed")),
        "dorm_trust_enabled": _as_bool(task.get("DormTrustEnabled")),
        "reception_message_board": _as_bool(task.get("ReceptionMessageBoard"), True),
        "reception_clue_exchange": _as_bool(task.get("ReceptionClueExchange"), True),
        "reception_send_clue": _as_bool(task.get("SendClue"), True),
        "continue_training": _as_bool(task.get("ContinueTraining")),
        "fiammetta_recovery_enabled": _as_bool(task.get("FiammettaRecoveryEnabled")),
        "fiammetta_targets": [
            str(task.get("FiammettaTarget1") or ""),
            str(task.get("FiammettaTarget2") or ""),
            str(task.get("FiammettaTarget3") or ""),
        ],
        "use_pinus_sylvestris": _as_bool(task.get("UsePinusSylvestris")),
        "use_perception_information": _as_bool(task.get("UsePerceptionInformation")),
        "use_worldly_plight": _as_bool(task.get("UseWorldlyPlight")),
        "use_abyssal_hunter": _as_bool(task.get("UseAbyssalHunter")),
    }
    if mode == 10000:
        spec.params["filename"] = str(task.get("Filename") or "")
        spec.params["plan_index"] = _as_int(task.get("PlanSelect"), -1)

    _consume(
        task,
        spec,
        "Mode",
        "RoomList",
        "UsesOfDrones",
        "DormThreshold",
        "OriginiumShardAutoReplenishment",
        "DormFilterNotStationed",
        "DormTrustEnabled",
        "ReceptionMessageBoard",
        "ReceptionClueExchange",
        "SendClue",
        "Filename",
        "PlanSelect",
        "CustomFileType",
        "ContinueTraining",
        "FiammettaRecoveryEnabled",
        "FiammettaTarget1",
        "FiammettaTarget2",
        "FiammettaTarget3",
        "UsePinusSylvestris",
        "UsePerceptionInformation",
        "UseWorldlyPlight",
        "UseAbyssalHunter",
    )
    return spec


def _map_fight(task: dict, ctx: dict, stage_override: str | None = None) -> TaskSpec:
    spec = _new_spec(task, "Fight")

    stage_plan = [str(s).strip() for s in _as_list(task.get("StagePlan")) if str(s).strip()]
    stage = stage_plan[0] if stage_plan else ""

    if not stage and _as_bool(task.get("UseCustomAnnihilation")):
        stage = str(task.get("AnnihilationStage") or "Annihilation")

    if stage_override is not None:
        spec.params["_stage_override_from"] = stage or "(空)"
        stage = stage_override

    spec.params.update(
        {
            "stage": stage,
            "medicine": _as_int(task.get("MedicineCount")) if _as_bool(task.get("UseMedicine")) else 0,
            # MaaCore 6.8 起 expiring_medicine 已废弃，改用 medicine_expire_days（可用临期药的天数阈值）
            "medicine_expire_days": (
                _as_int(task.get("MedicineExpireDays"), 2) if _as_bool(task.get("UseExpiringMedicine")) else 0
            ),
            "stone": _as_int(task.get("StoneCount")) if _as_bool(task.get("UseStone")) else 0,
            "times": _as_int(task.get("TimesLimit"), 2147483647)
            if _as_bool(task.get("EnableTimesLimit"))
            else 2147483647,
            "series": _as_int(task.get("Series"), 0),
            "DrGrandet": _as_bool(task.get("IsDrGrandet")),
            "client_type": ctx["client_type"],
            "server": ctx["server"],
            "report_to_penguin": ctx["report_to_penguin"],
            "report_to_yituliu": ctx["report_to_yituliu"],
        }
    )
    if ctx["penguin_id"]:
        spec.params["penguin_id"] = ctx["penguin_id"]
    if ctx["report_to_yituliu"] and ctx["yituliu_id"]:
        spec.params["yituliu_id"] = ctx["yituliu_id"]

    drop_id = task.get("DropId")
    if _as_bool(task.get("EnableTargetDrop")) and drop_id:
        spec.params["drops"] = {str(drop_id): _as_int(task.get("DropCount"), 1)}

    _consume(
        task,
        spec,
        "StagePlan",
        "UseMedicine",
        "MedicineCount",
        "UseStone",
        "StoneCount",
        "EnableTimesLimit",
        "TimesLimit",
        "Series",
        "IsDrGrandet",
        "UseExpiringMedicine",
        "MedicineExpireDays",
        "EnableTargetDrop",
        "DropId",
        "DropCount",
        "IsInventoryTarget",
        "UseCustomAnnihilation",
        "AnnihilationStage",
        "UseExpireMedicineForActivity",
        "StageResetMode",
        "UseOptionalStage",
        "UseStoneAllowSave",
        "HideUnavailableStage",
        "HideSeries",
        "IsStageManually",
    )
    return spec


def _map_mall(task: dict, ctx: dict) -> TaskSpec:
    spec = _new_spec(task, "Mall")
    spec.params = {
        "shopping": _as_bool(task.get("Shopping"), True),
        "visit_friends": _as_bool(task.get("VisitFriends"), True),
        "buy_first": split_list_field(task.get("FirstList")),
        "blacklist": split_list_field(task.get("BlackList")),
        "force_shopping_if_credit_full": _as_bool(task.get("ShoppingIgnoreBlackListWhenFull")),
        "only_buy_discount": _as_bool(task.get("OnlyBuyDiscount")),
        "reserve_max_credit": _as_bool(task.get("ReserveMaxCredit")),
        "credit_fight": _as_bool(task.get("CreditFight")),
        "formation_index": _as_int(task.get("CreditFightFormation"), 0),
    }
    _consume(
        task,
        spec,
        "Shopping",
        "VisitFriends",
        "FirstList",
        "BlackList",
        "ShoppingIgnoreBlackListWhenFull",
        "OnlyBuyDiscount",
        "ReserveMaxCredit",
        "CreditFight",
        "CreditFightFormation",
        "CreditFightLastTime",
        "VisitFriendsOnceADay",
        "VisitFriendsLastTime",
        "CreditFightOnceADay",
        "IsCreditFightAvailable",
        "IsVisitFriendsAvailable",
    )
    return spec


def _map_award(task: dict, ctx: dict) -> TaskSpec:
    spec = _new_spec(task, "Award")
    spec.params = {
        "award": _as_bool(task.get("Award"), True),
        "mail": _as_bool(task.get("Mail")),
        "recruit": _as_bool(task.get("FreeGacha")),
        "orundum": _as_bool(task.get("Orundum")),
        "mining": _as_bool(task.get("Mining")),
        "specialaccess": _as_bool(task.get("SpecialAccess")),
    }
    if task.get("SignInEvent") is not None:
        spec.params["signinevent"] = _as_bool(task.get("SignInEvent"))
    _consume(task, spec, "Award", "Mail", "FreeGacha", "Orundum", "Mining", "SpecialAccess", "SignInEvent")
    return spec


def _map_roguelike(task: dict, ctx: dict) -> TaskSpec:
    spec = _new_spec(task, "Roguelike")

    mode_raw = task.get("Mode")
    if isinstance(mode_raw, str):
        mode = {
            "Exp": 0,
            "Investment": 1,
            "Collectible": 4,
            "CollapsalParadigm": 5,
            "MonthlySquad": 6,
            "DeepExploration": 7,
        }.get(mode_raw, 0)
    else:
        mode = _as_int(mode_raw, 0)

    spec.params = {
        "theme": str(task.get("Theme") or "Phantom"),
        "mode": mode,
        "squad": str(task.get("Squad") or "指挥分队"),
        "roles": str(task.get("Roles") or "取长补短"),
        "starts_count": _as_int(task.get("StartCount"), 2147483647),
        "difficulty": _as_int(task.get("Difficulty"), 0),
        "investment_enabled": _as_bool(task.get("Investment"), True),
        "investments_count": _as_int(task.get("InvestCount"), 2147483647),
        "stop_at_final_boss": _as_bool(task.get("StopAtFinalBoss")),
        "stop_at_max_level": _as_bool(task.get("StopWhenLevelMax")),
        "stop_when_investment_full": _as_bool(task.get("StopWhenDepositFull")),
        "start_with_elite_two": _as_bool(task.get("StartWithEliteTwo")),
        "only_start_with_elite_two": _as_bool(task.get("StartWithEliteTwoOnly")),
        "use_nonfriend_support": _as_bool(task.get("UseSupportNonFriend")),
        "refresh_trader_with_dice": _as_bool(task.get("RefreshTraderWithDice")),
        "start_with_seed": _as_bool(task.get("StartWithSeed")),
        "collectible_mode_squad": str(task.get("CollectibleModeSquad") or ""),
        "collectible_mode_shopping": _as_bool(task.get("CollectibleShopping")),
        "monthly_squad_auto_iterate": _as_bool(task.get("MonthlySquadAutoIterate")),
        "monthly_squad_check_comms": _as_bool(task.get("MonthlySquadCheckComms")),
        "deep_exploration_auto_iterate": _as_bool(task.get("DeepExplorationAutoIterate")),
    }
    _consume(task, spec, *[k for k in task.keys() if k not in _META_KEYS])
    return spec


def _map_reclamation(task: dict, ctx: dict) -> TaskSpec:
    spec = _new_spec(task, "Reclamation")
    spec.params = {
        "theme": str(task.get("Theme") or "Fire"),
        "mode": _as_int(task.get("Mode"), 0),
        "tools_to_craft": [str(t) for t in _as_list(task.get("ToolToCraft")) if str(t).strip()],
        "increment_mode": _as_int(task.get("IncrementMode"), 0),
        "num_craft_batches": _as_int(task.get("MaxCraftCountPerRound"), 16),
    }
    _consume(task, spec, *[k for k in task.keys() if k not in _META_KEYS])
    return spec


_TASK_MAPPERS = {
    "StartUpTask": _map_startup,
    "RecruitTask": _map_recruit,
    "InfrastTask": _map_infrast,
    "MallTask": _map_mall,
    "AwardTask": _map_award,
    "RoguelikeTask": _map_roguelike,
    "ReclamationTask": _map_reclamation,
}

# GUI 里未启用但用户可能临时打开的额外开关（例如 CloseDown / 领取信用战）
_IGNORED_UNMAPPED = {
    "AccountSwitchEnabled",
}


def build_queue(cfg: dict, gui_cfg: dict | None = None) -> tuple[list[TaskSpec], list[str], list[str]]:
    """生成待提交的任务列表。

    返回 ``(tasks, warnings, notes)``：
      * ``tasks`` 只包含启用的任务；
      * ``warnings`` 是会影响行为的问题（例如 GUI 里开了脚本复现不了的功能）；
      * ``notes`` 只是提示（例如 MAA 新版本写入的默认值字段），不影响运行结论。
    """
    warnings: list[str] = []
    notes: list[str] = []
    task_cfg = cfg.get("tasks", {})

    override_file = str(task_cfg.get("override_file") or "").strip()
    if override_file:
        path = resolve_path(cfg, override_file)
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        specs: list[TaskSpec] = []
        for item in raw if isinstance(raw, list) else raw.get("tasks", []):
            specs.append(
                TaskSpec(
                    core_type=str(item["core_type"]),
                    params=dict(item.get("params") or {}),
                    gui_name=str(item.get("name") or item["core_type"]),
                    gui_type="override",
                )
            )
        return specs, warnings, notes

    if gui_cfg is None:
        gui_cfg = load_gui_config(cfg)

    ctx = _global_context(gui_cfg)
    current = ctx["config_name"]
    conf = (gui_cfg.get("Configurations") or {}).get(current) or {}
    queue = conf.get("TaskQueue") or []

    skip = {str(x) for x in _as_list(task_cfg.get("skip_tasks"))}
    stage_overrides = {str(k): str(v) for k, v in (task_cfg.get("stage_overrides") or {}).items()}
    used_overrides: set[str] = set()

    specs = []
    for task in queue:
        gui_type = str(task.get("$type") or task.get("TaskType") or "")
        name = str(task.get("Name") or gui_type)
        enabled = _as_bool(task.get("IsEnable"), True)

        if gui_type == "FightTask":
            if name in stage_overrides:
                used_overrides.add(name)
            spec = _map_fight(task, ctx, stage_override=stage_overrides.get(name))
        else:
            mapper = _TASK_MAPPERS.get(gui_type)
            if mapper is None:
                warnings.append(f"未识别的任务类型 {gui_type}（{name}），已跳过")
                continue
            spec = mapper(task, ctx)

        spec.enabled = enabled
        if name in skip:
            spec.enabled = False
            warnings.append(f"按配置跳过任务：{name}")

        leftover = [k for k in spec.unmapped if k not in _IGNORED_UNMAPPED]
        if leftover:
            meaningful, inert = _split_unmapped(task, leftover)
            if meaningful:
                warnings.append(
                    f"任务「{spec.label}」有未映射到 MaaCore 的 GUI 字段，且取值不是默认值，"
                    f"可能出现「界面行为 ≠ 脚本行为」：{', '.join(sorted(meaningful))}"
                )
            if inert:
                notes.append(
                    f"任务「{spec.label}」有未映射的 GUI 字段，但都是默认值（无影响）："
                    f"{', '.join(sorted(inert))}"
                )

        if spec.enabled:
            specs.append(spec)

    for unused in sorted(set(stage_overrides) - used_overrides):
        warnings.append(
            f"config.json 的 stage_overrides 里的「{unused}」没有匹配到任何理智作战任务"
            f"（任务名要和 MAA 界面里的一致），这一条不会生效"
        )

    return specs, warnings, notes


def _split_unmapped(task: dict, keys: list[str]) -> tuple[list[str], list[str]]:
    """把未映射的 GUI 字段分成「有影响」和「默认值无影响」两类。

    MAA 每次保存配置都会带上一些新字段（默认值），全部当警告会把报告刷成噪音；
    但取值非默认时就必须警告——那意味着界面上开了脚本复现不了的功能。
    """
    meaningful: list[str] = []
    inert: list[str] = []
    for key in keys:
        value = task.get(key)
        if key == "UseWeeklySchedule" and _as_bool(value):
            meaningful.append(f"{key}（MAA 的「周计划」在界面里换算成具体关卡/排班，脚本无法复现，请改用固定关卡或关掉它）")
            continue
        if value is None or value is False or value == "" or value == 0 or value == [] or value == {}:
            inert.append(key)
        else:
            meaningful.append(key)
    return meaningful, inert


def load_gui_config(cfg: dict) -> dict:
    gui_rel = cfg["maa"]["gui_config"]
    if Path(gui_rel).is_absolute():
        path = Path(gui_rel)
    else:
        path = resolve_path(cfg, str(Path(cfg["maa"]["install_dir"]) / gui_rel))
    return json.loads(path.read_text(encoding="utf-8-sig"))


# --------------------------------------------------------------------------------------
# 关卡有效性校验（防止活动结束后 YW-8 这类关卡失效）
# --------------------------------------------------------------------------------------


@dataclass
class StageCheck:
    stage: str
    ok: bool
    kind: str  # empty | activity | permanent | expired | unknown | unverified
    message: str
    activity: str = ""
    expire_at: str = ""

    def to_json(self) -> dict:
        return {
            "stage": self.stage,
            "ok": self.ok,
            "kind": self.kind,
            "message": self.message,
            "activity": self.activity,
            "expire_at": self.expire_at,
        }


def fetch_stage_activity(api_url: str, timeout: int = 10) -> tuple[dict | None, str]:
    """拉取 MAA 官方活动关卡接口：返回 (数据, 错误说明)。"""
    if not api_url:
        return None, "未配置接口地址"
    try:
        request = urllib.request.Request(api_url, headers={"User-Agent": "maa-arknights-auto"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return None, "接口返回格式不是对象"
        return payload, ""
    except Exception as exc:  # noqa: BLE001 - 网络问题不该让脚本崩
        return None, f"{type(exc).__name__}: {exc}"


def load_base_stage_codes(stages_json: Path) -> set[str]:
    """读取 MAA 基础关卡表 resource/stages.json 里的所有关卡代码（大写化便于比较）。"""
    try:
        data = json.loads(stages_json.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return set()
    codes: set[str] = set()
    items = data if isinstance(data, list) else list(data.values())
    for item in items:
        if isinstance(item, dict) and item.get("code"):
            codes.add(str(item["code"]).strip().upper())
    return codes


def _normalize_stage(stage: str) -> str:
    """去掉 MAA 支持的标准/磨难后缀，便于查表。"""
    text = (stage or "").strip()
    for suffix in ("-NORMAL", "-HARD"):
        if text.upper().endswith(suffix):
            text = text[: -len(suffix)]
    return text


def check_stage_availability(
    stage: str,
    client_type: str,
    activity_data: dict | None,
    base_stage_codes: set[str],
    api_error: str = "",
) -> StageCheck:
    """判断关卡是否还能打。

    判定顺序：空关卡 → 当前活动列表（含到期时间）→ MAA 基础关卡表 → 判定为不可用。
    这样活动结束后，像 YW-8 这种「只存在于活动增量资源」的关卡会被明确拦下，
    而不是让 MAA 跑到关卡界面里瞎试。
    """
    normalized = _normalize_stage(stage)
    if not normalized:
        return StageCheck(
            stage or "",
            True,
            "empty",
            "关卡为空：MAA 会识别「当前/上次关卡」，也就是账号上次打的那一关——不能保证是哪个关卡",
        )

    if activity_data:
        client = activity_data.get(client_type) or {}
        for activity in (client.get("sideStoryStage") or {}).values():
            info = activity.get("Activity") or {}
            for item in activity.get("Stages") or []:
                value = str(item.get("Value") or "").strip()
                if value.upper() != normalized.upper():
                    continue
                name = str(info.get("Tip") or info.get("StageName") or value)
                expire_text = str(info.get("UtcExpireTime") or "")
                try:
                    zone = timezone(timedelta(hours=float(info.get("TimeZone") or 8)))
                    expire = datetime.strptime(expire_text, "%Y/%m/%d %H:%M:%S").replace(tzinfo=zone)
                except (TypeError, ValueError):
                    return StageCheck(
                        normalized, True, "activity",
                        f"在当前活动「{name}」列表中找到该关卡（到期时间无法解析：{expire_text}）",
                        name, expire_text,
                    )
                if datetime.now(zone) > expire:
                    return StageCheck(
                        normalized, False, "expired",
                        f"活动「{name}」已于 {expire_text} 结束，该关卡不再可打", name, expire_text,
                    )
                return StageCheck(
                    normalized, True, "activity",
                    f"当前活动「{name}」，可用至 {expire_text}", name, expire_text,
                )

    if normalized.upper() in base_stage_codes:
        return StageCheck(normalized, True, "permanent", "MAA 基础关卡表中的常规/别传关卡，可长期使用")

    if not activity_data:
        return StageCheck(
            normalized, False, "unverified",
            f"既不在 MAA 基础关卡表中，也无法查询活动接口（{api_error or '未知原因'}），无法确认该关卡是否可打",
        )

    return StageCheck(
        normalized, False, "unknown",
        "既不在当前活动关卡列表中，也不在 MAA 基础关卡表中；通常是活动已结束（活动关卡只存在于增量资源里），或关卡名写错",
    )


if __name__ == "__main__":  # 便于手工检查映射结果
    import sys

    cfg_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("config.json")
    conf = load_config(cfg_path)
    specs, warns, note_list = build_queue(conf)
    print(f"配置：{conf['_config_path']}")
    print(f"任务队列（{len(specs)} 项）：")
    for i, spec in enumerate(specs, 1):
        print(f"  {i}. {spec.describe()}")
    for warn in warns:
        print(f"  [警告] {warn}")
    for note in note_list:
        print(f"  [提示] {note}")
