# -*- coding: utf-8 -*-
"""非文本消息识别的离线测试：用真实数据库里的消息 + 临时目录，不碰真实 inbox。"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from wechatauto import WeChatDB
from inbox import Inbox

_cfg_path = BASE / "config.json"
if not _cfg_path.exists():
    _cfg_path = BASE / "config.example.json"
cfg = json.load(open(_cfg_path, encoding="utf-8"))
db = WeChatDB(account=cfg.get("account"))
tmp = Path(tempfile.mkdtemp(prefix="inbox_test_"))
print("临时目录:", tmp)

# 找到用户发来的那几条真实消息
real = {}
for s in db.get_sessions(limit=40):
    u = s.get("username") or ""
    for m in db.get_messages(u, limit=40):
        t = m.get("type")
        if t != "文本" and t not in real:
            real[t] = m
print("真实样本类型:", list(real))

box = Inbox(tmp, stickers=None, media=None, db=db)

# [1] 表情包：能拿到 md5、能落地、能描述
emoji = real.get("动画表情")
assert emoji, "没找到表情包样本"
line = box.accept(emoji, emoji.get("sender_username") or "")
rec = box.items[0]
print("[1] 表情包 →", line, "| 落地:", bool(rec["file"]), "| md5:", rec["md5"][:8])
assert line.startswith("[他发来一个表情包"), "表情包描述不对: %s" % line
assert rec["md5"], "没取到 md5"
assert rec["file"] and Path(rec["file"]).exists(), "表情原图没落地"

# [2] 教她认识这张：加注释后同款能读出来
box.note(rec["md5"], "狗头，意思是反讽")
line2 = box.accept(emoji, emoji.get("sender_username") or "")
print("[2] 教过之后 →", line2)
assert "狗头" in line2, "注释没生效: %s" % line2

# [3] 缓存：第二次不该再下载（文件已存在）
size_before = Path(rec["file"]).stat().st_mtime
box.accept(emoji, emoji.get("sender_username") or "")
assert Path(rec["file"]).stat().st_mtime == size_before, "重复下载了"

# [4] 图片：media=None 时只留描述，不报错
img = real.get("图片")
if img:
    line3 = box.accept(img, img.get("sender_username") or "")
    print("[4] 图片 →", line3)
    assert line3 == "[他发来一张图片]", line3

# [5] 文件/链接/卡片
card = real.get("文件/链接/卡片")
if card:
    line4 = box.accept(card, card.get("sender_username") or "")
    print("[5] 卡片 →", line4)
    assert line4.startswith("[他发来一张卡片"), line4

# [6] 造几条构造消息：语音 / 视频 / 位置 / 文件 / 系统消息
cases = [
    ({"type": "语音", "content": '<msg><voicemsg voicelength="4200" /></msg>'},
     "[他发来一条语音，4 秒]"),
    ({"type": "视频", "content": "<msg><videomsg /></msg>"}, "[他发来一段视频]"),
    ({"type": "位置", "content": "<msg><location /></msg>"}, "[他发来一个位置]"),
    ({"type": "文件/链接/卡片",
      "content": '<msg><appmsg type="6"><title>作品集.pdf</title></appmsg></msg>'},
     "[他发来一个文件：作品集.pdf]"),
    ({"type": "系统消息", "content": "<msg />"}, None),
    ({"type": "音视频通话", "content": "<msg />"}, None),
]
for m, want in cases:
    m["sender_username"] = "wxid_test"
    got = box.accept(m, "wxid_test")
    print("    %-14s → %s" % (m["type"], got))
    assert got == want, "%s 期望 %r 得到 %r" % (m["type"], want, got)

# [7] 纯表情包不能被静默：没有文字时一定回
import bridge as B

B.BASE = tmp
B.STATE_PATH = tmp / "state.json"      # 别把假会话写进真实 state.json
br = B.Bridge(dict(cfg))
br.inbox = box
br.stickers = None
br.behavior = dict(cfg.get("behavior") or {})
br.cfg["pacing"] = dict(cfg["pacing"], silence_probability=1.0)
print("[7] 没有文字时是否静默:", br._should_stay_silent("wxid_test", []))
assert br._should_stay_silent("wxid_test", []) is False, "只发了个表情包就被无视了"

# [8] 完整链路：pending 里只有事件、没有文字，也要走到回复
sent = []
br.db = type("DB", (), {"get_nickname": staticmethod(lambda u: "测试")})()
br.send_bubbles = lambda u, b, n="": sent.append(b) or True
br.reply_to = lambda u, n, t, multi=False: (sent.append(t) or ["哈哈这个我看不懂"])[:1]
br.sent_times = {}
br.send_log = []
br._silence_streak = {}
br.pending = {"wxid_test": {"texts": [], "parts": ["[他发来一个表情包：狗头，意思是反讽]"],
                            "first_at": time.time() - 100, "last_at": time.time() - 100, "wait": 0, "n": 1}}
br.maybe_answer_pending()
print("[8] 纯表情包走完链路，模型收到的:", sent[0] if sent else None)
assert sent, "纯表情包的 pending 被丢了"
assert "狗头" in str(sent[0]), "提示没传到模型"
assert br.pending == {}, "pending 没清干净"

# [9] 文字 + 表情包混合，顺序要保留
br.sent_times = {}          # 否则会被"防连发"间隔拦住，那是另一条逻辑
br.pending = {"wxid_test": {"texts": ["看这个"], "parts": ["看这个", "[他发来一个表情包：无语]"],
                            "first_at": time.time() - 100, "last_at": time.time() - 100, "wait": 0, "n": 2}}
sent.clear()
br.maybe_answer_pending()
got = str(sent[0]) if sent else ""
print("[9] 混合顺序:", got.replace("\n", " ⏎ "))
assert got.index("看这个") < got.index("[他发来"), "顺序乱了"

# [10] 控制台能看到记录，注释能删
st = box.status()
print("[10] 收件箱状态:", st["count"], "条", st["kinds"], "| 认识", st["known"], "张")
assert st["count"] >= 4, st
assert box.forget_note(rec["md5"]) is True
assert box.forget_note(rec["md5"]) is False

# [11] handle_chat：以前那行 type=="文本" 会把表情包整个扔掉
class FakeDB:
    def __init__(self, msgs):
        self.msgs = msgs

    def get_messages(self, u, limit=20):
        return self.msgs

    def get_nickname(self, u):
        return "测试"


br.db = FakeDB([
    {"type": "动画表情", "sort_seq": 5, "sender_username": "wxid_test", "local_id": 3,
     "content": '<msg><emoji md5="deadbeef" cdnurl="" /></msg>'},
    {"type": "文本", "sort_seq": 4, "sender_username": "wxid_test", "local_id": 2,
     "content": "看这个"},
    {"type": "文本", "sort_seq": 3, "sender_username": "wxid_other", "local_id": 1,
     "content": "别人的话不该收"},
])
br.state["watermarks"]["wxid_test"] = 0
br.pending = {}
br.handle_chat("wxid_test")
item = br.pending.get("wxid_test") or {}
parts = item.get("parts") or []
print("[11] handle_chat 收到:", parts, "| 水位:", br.state["watermarks"]["wxid_test"])
assert "看这个" in parts, "文字漏了"
assert any(str(p).startswith("[他发来一个表情包") for p in parts), "表情包又被扔了"
assert not any("别人的话" in str(p) for p in parts), "把别人的消息收进来了"
assert br.state["watermarks"]["wxid_test"] == 5

shutil.rmtree(tmp, ignore_errors=True)
print("\n全部通过，临时目录已清")
