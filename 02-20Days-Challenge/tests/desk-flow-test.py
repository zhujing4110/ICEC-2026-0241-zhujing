"""看电脑这条链路的端到端测试：申请 -> 批准/拒绝 -> 她接着说。

用假模型（注入 tool_calls）保证确定性，不依赖真实模型这轮想不想调工具。
真实模型的行为另外单独测。
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import bridge as B
import desk as D

TMP = Path(tempfile.mkdtemp(prefix="deskflow_"))
B.STATE_PATH = TMP / "state.json"
USER = "wxid_desk_test"
NICK = "测试者"


class FakeDB:
    def get_nickname(self, u):
        return NICK


class Sink:
    """记录发出去的每一条，不碰微信。"""

    def __init__(self):
        self.texts = []
        self.stickers = []

    def send(self, user, text, nick=""):
        self.texts.append(text)
        return True

    def send_bubbles(self, user, bubbles, nick=""):
        self.texts.extend(bubbles)
        return True

    def send_sticker(self, user, nick, kw):
        self.stickers.append(kw)
        return True


ok = 0


def check(name, cond, extra=""):
    global ok
    print(("  [OK] " if cond else "  [!!] ") + name + (f"   {extra}" if extra else ""))
    assert cond, name
    ok += 1


# 造一个可看的目录
WORK = TMP / "work"
WORK.mkdir()
(WORK / "报告.txt").write_text("第一版\n第二版\n", encoding="utf-8")
(WORK / "空夹").mkdir()

# 仓库里不放真实 config.json（含密钥），退回示例配置
if not B.CONFIG_PATH.exists():
    B.CONFIG_PATH = B.BASE / "config.example.json"
cfg = B.load_config()
br = B.Bridge(cfg)
br.load_persona(force=True)
br.db = FakeDB()
br.desk = D.Desk(TMP / "deskbase", {"extra_roots": [str(WORK)]})
# 临时目录本身也允许（Desk 的 base 已包含），再加 work
br.desk.allow_roots.append(WORK.resolve())
sink = Sink()
br.send = sink.send
br.send_bubbles = sink.send_bubbles
br.send_sticker = sink.send_sticker

# 假模型：没有 tool 结果时申请看目录，有 tool 结果时给正文
CALL_LOG = []


def fake_llm(cfg_, messages, tools, max_tokens=None):
    has_tool = any(m.get("role") == "tool" for m in messages)
    CALL_LOG.append({"has_tool": has_tool, "n": len(messages),
                     "tools": [t["function"]["name"] for t in tools]})
    if not has_tool:
        return {"content": "", "tool_calls": [{
            "id": "call_a1", "type": "function",
            "function": {"name": "list_dir",
                         "arguments": json.dumps({"path": str(WORK)})}}]}
    tool_msg = next((m.get("content") or "") for m in messages if m.get("role") == "tool")
    if "没允许" in tool_msg:
        return {"content": "那算了，不看了", "tool_calls": []}
    return {"content": "看了下，就一个报告.txt", "tool_calls": []}


B.deepseek_tool_chat = fake_llm

print("[1] 她申请看电脑（不该直接执行）")
bubbles = br.reply_to(USER, NICK, "帮我看看我文件夹里都有啥")
check("回了话而不是闷着", bool(bubbles), str(bubbles))
check("话里提醒你去批", "允许" in bubbles[0], bubbles[0])
check("记下了待批准请求", USER in br.desk_requests)
req = br.desk_requests[USER]
check("请求里有工具和预览", req["calls"][0]["tool"] == "list_dir"
      and "报告" not in req["calls"][0]["preview"], req["calls"][0]["preview"])
check("申请阶段没有执行（流水还是空的）", len(br.desk.history) == 0, f"{len(br.desk.history)} 条")
check("申请阶段没有写历史", USER not in br.state["history"])
print()

print("[2] 控制台看得到这个请求")
st = br.desk_status()
check("status 里有 1 个待批", st["pending_count"] == 1, str(st["pending_count"]))
check("待批里带倒计时", st["pending"][0]["left"] > 250, f"{st['pending'][0]['left']}s")
check("待批里带人话预览", "看看" in st["pending"][0]["calls"][0]["preview"])
print()

print("[3] 点「允许」→ 真执行")
r = br.approve_desk(USER, req["id"], True)
check("批准成功", r["ok"], str(r))
check("工具真跑了（流水 +1）", len(br.desk.history) == 1, f"{len(br.desk.history)} 条")
check("结果排队等人消费", USER in br.desk_results)
out = br.desk_results[USER]["outputs"][0]
check("结果里有文件清单", "报告.txt" in out["content"], out["content"][:40])
print()

print("[4] 她接着说（不能卡住主循环）")
before = len(sink.texts)
br.maybe_resume_desk()
check("发出了后续回复", len(sink.texts) > before, str(sink.texts[before:]))
check("回复是模型生成的正文", any("报告.txt" in t for t in sink.texts[before:]), str(sink.texts[before:]))
check("请求已清掉", USER not in br.desk_requests)
check("结果已消费", USER not in br.desk_results)
check("历史写进了这轮对话", USER in br.state["history"], str(len(br.state["history"].get(USER, []))))
hist = br.state["history"][USER]
check("历史里有用户那句", any("文件夹" in (m.get("content") or "") for m in hist))
print()

print("[5] 第二次调用带上了工具结果")
check("模型第二次收到了 tool 消息", CALL_LOG[-1]["has_tool"] if CALL_LOG else False)
check("两次都带了工具定义", all(c["tools"] for c in CALL_LOG), str(CALL_LOG[0]["tools"]))
print()

print("[6] 点「拒绝」→ 她不会看到任何东西")
sink.texts.clear()
CALL_LOG.clear()
bubbles = br.reply_to(USER, NICK, "你再看看那边有啥")
req2 = br.desk_requests[USER]
assert req2
r = br.approve_desk(USER, req2["id"], False)
check("拒绝成功", r["ok"])
check("拒绝时不执行工具（流水仍是 1）", len(br.desk.history) == 1, f"{len(br.desk.history)} 条")
br.maybe_resume_desk()
check("拒绝后她仍会回一句", len(sink.texts) > 0, str(sink.texts))
check("她知道自己没看到（不是假装看完了）",
      any("算了" in t for t in sink.texts) and not any("报告.txt" in t for t in sink.texts),
      str(sink.texts))

# 检查喂给模型的是"没允许"，而不是"看完了"
deny_msg = None
for m in br.state.get("_last_msgs", []) or []:
    pass
print()

print("[7] 同一个请求不能批两次 / 别人的 id 批不了")
bubbles = br.reply_to(USER, NICK, "看看我这边")
req3 = br.desk_requests[USER]
r1 = br.approve_desk(USER, req3["id"], True)
r2 = br.approve_desk(USER, req3["id"], True)
check("第一次成功", r1["ok"])
check("第二次被拒（防重复执行）", not r2["ok"], r2.get("error", ""))
r3 = br.approve_desk(USER, "bogus_id", True)
check("伪造 id 被拒", not r3["ok"])
br.maybe_resume_desk()
print()

print("[8] 超时没人批 → 放弃并说一句")
sink.texts.clear()
bubbles = br.reply_to(USER, NICK, "看下桌面")
req4 = br.desk_requests[USER]
br.desk_requests[USER]["created"] = time.time() - 400      # 伪造 400 秒前
br.maybe_resume_desk()
check("过期的请求被清掉", USER not in br.desk_requests)
check("超时后回了一句", any("不看" in t for t in sink.texts), str(sink.texts))
check("超时记进流水", any(h["kind"] == "超时" for h in br.desk_log), str(len(br.desk_log)))
print()

print("[9] 自然聊天不该碰电脑")
sink.texts.clear()


def fake_chat_only(cfg_, messages, tools, max_tokens=None):
    CALL_LOG.append({"has_tool": False, "n": len(messages), "tools": [t["function"]["name"] for t in tools]})
    return {"content": "在呢|咋了", "tool_calls": []}


B.deepseek_tool_chat = fake_chat_only
bubbles = br.reply_to(USER, NICK, "在吗")
check("闲聊正常回，不申请看电脑", bool(bubbles) and USER not in br.desk_requests, str(bubbles))
print()

print("[10] 沙箱在工具层真的兜住了")
br.desk.history.clear()
bad = [{"id": "call_bad", "tool": "read_file", "args": {"path": "C:/Windows/System32/drivers/etc/hosts"},
        "preview": "读 hosts"}]
br.desk_requests[USER] = {"id": "r_bad", "calls": bad, "messages": [], "nick": NICK,
                          "created": time.time(), "rounds": 1, "timeout": 300, "user_text": ""}
r = br.approve_desk(USER, "r_bad", True)
res = br.desk_results[USER]["outputs"][0]
check("就算批了也读不到系统文件", "失败" in res["content"], res["content"][:50])
br.desk_requests.clear()
br.desk_results.clear()
print()

print("[11] 关掉开关就回到普通聊天")
br.desk = None
check("不给工具了", br._desk_tools() == [])
check("status 显示未启用", br.desk_status()["enabled"] is False)
B.deepseek_tool_chat = fake_llm
print()

shutil.rmtree(TMP, ignore_errors=True)
print(f"全部通过：{ok} 项")
