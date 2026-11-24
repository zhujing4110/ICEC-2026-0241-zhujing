# -*- coding: utf-8 -*-
"""离线验证节奏逻辑：不连微信、不调模型。"""
import sys, time, random
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(BASE))

import bridge as B

B.BASE = BASE
# 仓库里不放真实 config.json（含密钥），退回示例配置
if not B.CONFIG_PATH.exists():
    B.CONFIG_PATH = BASE / "config.example.json"
# 用一个临时 state 文件，绝不碰真实的 state.json（水位和对话历史都在里面）
import tempfile
B.STATE_PATH = Path(tempfile.gettempdir()) / "_wxbot_pacing_test_state.json"
if B.STATE_PATH.exists():
    B.STATE_PATH.unlink()
random.seed(7)

cfg = B.load_config()

# ---------- 1. 纯应答判定
cases = [
    ("嗯", True), ("嗯嗯", True), ("好的", True), ("收到", True), ("哈哈", True),
    ("谢谢", True), ("行吧", True), ("OK", True),
    ("在吗", False), ("怎么了", False), ("还行", False), ("不行", False),
    ("嗯，那明天几点", False), ("我想问个事", False), ("配色怎么弄", False),
]
bad = [(t, e) for t, e in cases if B.is_low_content(t) != e]
print("[1] is_low_content:", "全部通过" if not bad else f"失败 {bad}")

# ---------- 2. 等待与思考时长
random.seed(3)
pcfg = cfg["pacing"]
settles = [B.settle_delay(pcfg, n) for n in (1, 1, 1, 2, 3, 5)]
print("[2] settle(1条) 样点:", [round(x, 1) for x in settles[:3]],
      "| 3条:", round(B.settle_delay(pcfg, 3), 1),
      f"| 上限 {pcfg['max_settle_seconds']}s")
assert all(x <= pcfg["max_settle_seconds"] for x in settles)

th = [B.think_delay(pcfg, "嗯") for _ in range(3)] + [B.think_delay(pcfg, "这条消息比较长" * 6)]
print("[2] think 样例:", [round(x, 1) for x in th], f"| 上限 {pcfg['think_max']}s")
assert all(x <= pcfg["think_max"] for x in th)


# ---------- 3. 假 Bridge：连发三条 → 只回一次
class FakeDB:
    def __init__(self):
        self.msgs = []
        self.seq = 0

    def push(self, user, text):
        self.seq += 1
        self.msgs.append({
            "sort_seq": self.seq, "sender_username": user,
            "type": "文本", "content": text,
            "create_time": time.time(),
        })

    def get_messages(self, user, limit=20):
        return [m for m in self.msgs][-limit:]

    def get_nickname(self, user):
        return "测试人"


br = B.Bridge(cfg)
br.db = FakeDB()
br.paused = False
br.send_log = []

replies = []


def fake_reply_to(user, nick, text, multi=False):
    replies.append({"text": text, "multi": multi})
    return ["行|我看看"]


def fake_send_bubbles(user, bubbles, nick=""):
    br.sent_times[user] = time.time()
    br.send_log.append(time.time())
    return True


br.reply_to = fake_reply_to
br.send_bubbles = fake_send_bubbles
br.remember_async = lambda *a, **k: None
br.learn_async = lambda *a, **k: None

USER = "wxid_test"
br.state["watermarks"][USER] = 0   # 已有水位，走正常分支

# t=0 第一条
br.db.push(USER, "我想问个事")
br.handle_chat(USER)
print("[3] 收到第1条后 pending:", len(br.pending.get(USER, {}).get("texts", [])))

# t≈2s 第二条（他还在打）
time.sleep(2)
br.db.push(USER, "就是那个")
br.handle_chat(USER)

# t≈4s 第三条
time.sleep(2)
br.db.push(USER, "配色怎么弄")
br.handle_chat(USER)
print("[3] 攒到:", br.pending[USER]["texts"])

# 立刻扫一轮：不该回
br.maybe_answer_pending()
assert not replies, "他刚说完就回了，说明没等"
print("[3] 刚说完立刻扫描 → 没回（对）")

# 等够时间再扫
time.sleep(cfg["pacing"]["max_settle_seconds"] + 1)
br.maybe_answer_pending()
print("[3] 安静之后 → 回了", len(replies), "次")
if replies:
    r = replies[0]
    print("    收到的合并文本:", repr(r["text"]))
    print("    multi 标记:", r["multi"])
    assert r["multi"] is True, "三条合并没有标记 multi"
    assert r["text"].count("\n") == 2, "三条没合并全"
assert len(replies) == 1, f"回了 {len(replies)} 次，应该只回 1 次"
print("[3] 通过：三条只回一次")

# ---------- 4. 纯应答的静默 + 连续上限
br2 = B.Bridge(cfg)
br2.db = FakeDB()
br2.paused = False
br2.send_log = []
hit = []
br2.reply_to = lambda u, n, t, multi=False: hit.append(t) or ["嗯"]
br2.send_bubbles = lambda u, b, n="": True
br2.remember_async = lambda *a, **k: None
br2.learn_async = lambda *a, **k: None
br2.state["watermarks"][USER] = 0

# 概率钉死成 1.0，把随机剔除，专测"规则"本身
br2.cfg["pacing"] = dict(br2.cfg["pacing"], silence_probability=1.0, silence_max_streak=1)
br2._silence_streak[USER] = 0
seq = [br2._should_stay_silent(USER, ["嗯"]) for _ in range(4)]
print("[4] 必中概率下连续4次「嗯」:", seq)
assert seq[0] is True, "纯应答该静默却没静默"
assert seq[1] is False, "连着两次都不接，超了 silence_max_streak"
assert not any(seq[i] and seq[i + 1] for i in range(len(seq) - 1)), "出现了连续两次不接"

# 走完整链路：判定静默时真的一条都不发，判定该回时真发出去
sent = []
br2.send_bubbles = lambda u, b, n="": sent.append(b) or True
br2.reply_to = lambda u, n, t, multi=False: ["嗯嗯"]
br2._silence_streak[USER] = 0
br2._answer(USER, ["嗯"])          # 第 1 次：静默
br2._answer(USER, ["嗯"])          # 第 2 次：必回
print("[4] 完整链路 静默→必回 的实际发送数:", len(sent), sent)
assert len(sent) == 1, f"应该是静默一次、回一次，实际发了 {len(sent)} 次"

# 有内容 / 连发 一定不静默（概率仍然是 1.0，最严苛的条件）
assert not br2._should_stay_silent(USER, ["帮我看看这个配色"]), "正常消息被静默了"
assert not br2._should_stay_silent(USER, ["嗯", "算了还是明天弄"]), "连发被静默了"
print("[4] 通过：只有纯应答可能不接，且不会连着不接")

# ---------- 5. 开关关掉后行为回到"收到就回"
cfg_off = B.load_config()
cfg_off["pacing"] = dict(cfg_off.get("pacing") or {}, enabled=False)
br3 = B.Bridge(cfg_off)
br3.db = FakeDB()
br3.paused = False
br3.send_log = []
br3.reply_to = lambda u, n, t, multi=False: ["好"]
br3.send_bubbles = lambda u, b, n="": True
br3.remember_async = lambda *a, **k: None
br3.learn_async = lambda *a, **k: None
br3.state["watermarks"][USER] = 0
br3.db.push(USER, "测试")
br3.handle_chat(USER)
br3.maybe_answer_pending()
assert not br3.pending, "开关关掉后还在攒消息"
print("[5] 通过：pacing.enabled=false 时立即回复")

print("\n全部通过")
