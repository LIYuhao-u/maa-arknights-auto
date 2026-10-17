# -*- coding: utf-8 -*-
# 作者: leo <1193725859@qq.com> · 创建 2026-10-13 · 最近修改 2026-10-18
# 项目: maa-arknights-auto（MIT License） · 见 README.md / AUTHORS.md

"""生成「轻量全链路验证」用的任务清单与配置（一次性工具，产出文件不入库）。

- tasks.verify.json   : 只保留 StartUp / Award / Mall，参数从真实映射里取，保证与正式运行一致
- config.verify.json  : config.json 的副本，改为使用上面的任务清单并收紧超时
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # tests/
ROOT = HERE.parent                              # 仓库根目录
sys.path.insert(0, str(ROOT))

import maa_config as mc  # noqa: E402

cfg = mc.load_config(ROOT / "config.json")
specs, warnings, _notes = mc.build_queue(cfg)

ORDER = {"StartUp": 0, "Award": 1, "Mall": 2}
picked = [spec for spec in specs if spec.core_type in ORDER]
picked.sort(key=lambda spec: ORDER[spec.core_type])

override = [
    {
        "name": spec.gui_name,
        "core_type": spec.core_type,
        "params": {k: v for k, v in spec.params.items() if not k.startswith("_")},
    }
    for spec in picked
]
(HERE / "tasks.verify.json").write_text(
    json.dumps(override, ensure_ascii=False, indent=2), encoding="utf-8"
)

raw = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
raw["tasks"]["override_file"] = r"tests\tasks.verify.json"
raw["run"]["task_timeout_minutes"] = 25
raw["run"]["stall_timeout_minutes"] = 8
(HERE / "config.verify.json").write_text(
    json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
)

print("任务清单：")
for item in override:
    print("  " + item["name"] + " -> " + json.dumps(item["params"], ensure_ascii=False))
print("映射警告：" + (str(warnings) if warnings else "无"))
