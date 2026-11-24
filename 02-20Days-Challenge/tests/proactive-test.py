#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""主动发消息自测：读真实微信库 + 真实人设，默认只打印、不发送。

用法：
  python proactive-test.py                  # 现在这一刻会不会发，为什么
  python proactive-test.py --at 08:30       # 模拟早上 8:30
  python proactive-test.py --at 22:10       # 模拟晚上 22:10
  python proactive-test.py --at 15:30       # 模拟下午（只会触发话题延续）
  python proactive-test.py --gen morning    # 强制生成一条早安看看文风
  python proactive-test.py --send           # 真的发出去（谨慎）

注意：state.json 只在内存里改，不会写回；只有加 --send 才会发消息。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(BASE))

import bridge as B  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", help="模拟时间 HH:MM，不填用当前时间")
    ap.add_argument("--gen", choices=["morning", "evening", "followup"],
                    help="强制生成某类文案（跳过时间判定）")
    ap.add_argument("--send", action="store_true", help="真的发出去，默认只打印")
    args = ap.parse_args()

    B.setup_logging()
    # 仓库里不放真实 config.json（含密钥），退回示例配置
    if not B.CONFIG_PATH.exists():
        B.CONFIG_PATH = BASE / "config.example.json"
    cfg = B.load_config()
    br = B.Bridge(cfg)
    br.state = copy.deepcopy(B.load_state())

    from wechatauto import WeChatDB
    br.db = WeChatDB(account=cfg.get("account"))
    br.load_persona(force=True)
    br.init_knowledge()

    user = br._proactive_target()
    if not user:
        print("没有可主动发起的会话（state.json 里没历史记录）")
        return 0
    nick = br.db.get_nickname(user) or user
    print(f"\n目标会话: {nick} ({user})")

    if args.at:
        try:
            h, m = args.at.split(":")
            now = datetime.now().replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        except ValueError:
            print("--at 格式应为 HH:MM，例如 08:30")
            return 1
    else:
        now = datetime.now()

    last_t, max_seq, last_from_user = br._chat_stats(user)
    print(f"模拟时间: {now:%Y-%m-%d %H:%M}")
    if last_t:
        print(f"最后一条消息: {datetime.fromtimestamp(last_t):%m-%d %H:%M} "
              f"（距今 {(now.timestamp() - last_t) / 60:.0f} 分钟）")
    else:
        print("最后一条消息: 读不到")
    print(f"水位 max_seq={max_seq}  对方最后 seq={last_from_user}")

    kind, why = br.proactive_decision(now, user)
    print(f"判定: {kind or '不发'}   原因: {why}")
    rec = (br.state.get("proactive") or {}).get(user)
    print(f"当日记录: {json.dumps(rec, ensure_ascii=False) if rec else '(空)'}")

    target = args.gen or kind
    if not target:
        print("\n不生成文案。想单看文风可加 --gen morning / evening / followup")
        return 0

    print(f"\n--- 生成 {target} 文案 ---")
    text = br._gen_proactive(user, nick, target, now=now)
    print(text or "(生成失败)")

    if args.send and text:
        print("\n--- 真实发送 ---")
        print("发送结果:", "成功" if br.send(user, text, nick) else "失败")
    return 0


if __name__ == "__main__":
    sys.exit(main())
