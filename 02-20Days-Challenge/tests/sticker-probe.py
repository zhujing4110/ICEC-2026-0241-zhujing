# -*- coding: utf-8 -*-
"""测：同一批情绪场景下，她主动输出 STICKER 的频率。

只调模型，不发消息、不写 state。用来定位"为什么很少发图"。
"""
import re
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(BASE))
import bridge as B

B.BASE = BASE
# 仓库里不放真实 config.json（含密钥），退回示例配置
if not B.CONFIG_PATH.exists():
    B.CONFIG_PATH = BASE / "config.example.json"
B.STATE_PATH = Path(tempfile.gettempdir()) / "_wxbot_sticker_probe_state.json"

cfg = B.load_config()
br = B.Bridge(cfg)
br.load_persona(force=True)
br.init_stickers()          # 不初始化的话 build_system_prompt 不会注入标签列表，
                            # 模型只能凭 persona 猜词 —— 那样测出来的不是真实条件
print("库里标签:", list((br.stickers.stats().get("tags") or {}).keys()))

# 这些场景人一般会配图：吐槽、无奈、累、好笑、社死
CASES = [
    "今天累死了，加班到十点",
    "老板又改需求了，第三版了",
    "我猫把我键盘上的水杯打翻了",
    "刚在电梯里跟人撞了个满怀，好尴尬",
    "我终于把那个 bug 找出来了哈哈哈",
    "明天要早起，但现在已经一点了",
    "食堂今天的菜难吃到离谱",
    "我画的图被老师当范例展示了",
    "地铁上有人外放抖音，声音巨大",
    "刚睡醒，脑子还是糊的",
]

hit = 0
rows = []
for text in CASES:
    sys_p = br.build_system_prompt(text, "probe")
    raw = B.deepseek_chat(cfg, [
        {"role": "system", "content": sys_p},
        {"role": "user", "content": text},
    ])
    clean, st = B.extract_sticker(raw or "")
    picked = None
    if st:
        hit += 1
        got = br.stickers.pick(st)
        picked = (got or {}).get("tags")
    rows.append((text, (clean or "").replace("\n", " ")[:40], st, picked))

for t, c, s, p in rows:
    mark = "[图]" if s else "    "
    extra = f"   STICKER:{s}" if s else ""
    if s and not p:
        extra += "  ← 库里没有对应图"
    elif p:
        extra += f"  → {p}"
    print(f"  {mark} {t[:22]:<24} → {c}{extra}")

print(f"\n命中 {hit}/{len(CASES)} = {hit/len(CASES)*100:.0f}%")
