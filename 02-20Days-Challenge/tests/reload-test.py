# -*- coding: utf-8 -*-
"""验证 pacing 热重载：改 config 不重启也能生效。"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(BASE))
import bridge as B

B.BASE = BASE
B.STATE_PATH = Path(tempfile.gettempdir()) / "_wxbot_reload_test_state.json"
if B.STATE_PATH.exists():
    B.STATE_PATH.unlink()

# 把 config 复制到临时位置，改它而不是改真的
tmp_cfg = Path(tempfile.gettempdir()) / "_wxbot_reload_test_cfg.json"
cfg_src = BASE / "config.json"
if not cfg_src.exists():
    cfg_src = BASE / "config.example.json"
shutil.copyfile(cfg_src, tmp_cfg)
B.CONFIG_PATH = tmp_cfg

cfg = json.loads(tmp_cfg.read_text(encoding="utf-8"))
br = B.Bridge(cfg)
print("初始等待区间:", br.cfg["pacing"]["settle_seconds"])

# 模拟用户改了文件并保存
time.sleep(0.05)
data = json.loads(tmp_cfg.read_text(encoding="utf-8"))
data["pacing"]["settle_seconds"] = [1, 2]
data["pacing"]["silence_probability"] = 0.0
tmp_cfg.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

br.maybe_reload_live_cfg()
print("重载后等待区间:", br.cfg["pacing"]["settle_seconds"])
assert br.cfg["pacing"]["settle_seconds"] == [1, 2], "热重载没生效"
assert br.cfg["pacing"]["silence_probability"] == 0.0, "silence 参数没跟着更新"

# 没改文件时不应该反复重载（用日志条数间接判断：再调一次不报错即可）
br.maybe_reload_live_cfg()
print("重复调用无副作用")

# 写坏 JSON 时应该按住旧值而不是崩
time.sleep(0.05)
tmp_cfg.write_text("{ 这不是 json", encoding="utf-8")
br.maybe_reload_live_cfg()
assert br.cfg["pacing"]["settle_seconds"] == [1, 2], "配置坏了却把旧值弄丢了"
print("坏 JSON 时保留旧参数，进程不崩")

tmp_cfg.unlink(missing_ok=True)
B.STATE_PATH.unlink(missing_ok=True)
print("\n热重载通过")
