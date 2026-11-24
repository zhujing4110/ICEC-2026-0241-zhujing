#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
微信小号 AI 自动回复桥接

链路：好友/主号发消息 -> 解密本地微信数据库读到 -> DeepSeek 生成回复 -> 驱动微信窗口发出

只用小号（config.json 里 account 指定的那个）的数据库，不碰其他账号。
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import random
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from desk import Desk, build_tools_spec

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
STATE_PATH = BASE / "state.json"
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)

log = logging.getLogger("bridge")

# ---------------------------------------------------------------- 日志

def setup_logging() -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / "bridge.log", maxBytes=3 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    log.addHandler(fh)
    # 用 pythonw 挂在后台时没有控制台，sys.stdout 是 None，
    # StreamHandler 会在每条日志上报 AttributeError，看起来像"卡死但没报错"。
    if sys.stdout is not None:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)

# ---------------------------------------------------------------- 配置

def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    env_key = os.environ.get("DEEPSEEK_API_KEY")
    if env_key:
        cfg["deepseek"]["api_key"] = env_key
    return cfg

# ---------------------------------------------------------------- 状态

def load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.warning("state.json 损坏，重建")
    return {"watermarks": {}, "history": {}}

def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    tmp.replace(STATE_PATH)

# ---------------------------------------------------------------- 账号与窗口

def find_account_windows() -> Dict[str, int]:
    """返回 {account 目录名: hwnd}，按窗口进程实际打开的 db_storage 归属账号。"""
    import psutil
    import win32gui
    import win32process
    from wechatauto.db import auto_detect_db_dir

    root = Path(auto_detect_db_dir()).resolve()
    found: Dict[str, int] = {}

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        if "微信" not in win32gui.GetWindowText(hwnd):
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            opened = psutil.Process(pid).open_files()
        except Exception:
            return
        acc: Set[str] = set()
        for item in opened:
            try:
                parts = Path(item.path).resolve().relative_to(root).parts
            except ValueError:
                continue
            if len(parts) >= 3 and parts[1].lower() == "db_storage":
                acc.add(parts[0])
        if len(acc) == 1:
            found[acc.pop()] = hwnd

    win32gui.EnumWindows(cb, None)
    return found

# ---------------------------------------------------------------- 会话过滤

SKIP_EXACT = {
    "filehelper", "newsapp", "brandsessionholder", "notifymessage", "weixin",
    "fmessage", "medianote", "floatbottle", "qqmail", "tmessage", "qmessage",
    "officialaccounts", "@placeholder_foldgroup", "mphelper",
}
SKIP_SUFFIX = ("@chatroom", "@openim", "@weclaw", "@im.bot", "@app")
SKIP_PREFIX = ("gh_", "@", "weixin")

def is_replyable(username: str) -> bool:
    if not username:
        return False
    u = username.lower()
    if u in SKIP_EXACT:
        return False
    if u.endswith(SKIP_SUFFIX):
        return False
    if u.startswith(SKIP_PREFIX):
        return False
    return True

# ---------------------------------------------------------------- DeepSeek

def deepseek_chat(cfg: Dict[str, Any], messages: List[Dict[str, str]],
                  max_tokens: Optional[int] = None) -> str:
    d = cfg["deepseek"]
    # 预算太小会被思考链吃光：finish_reason=length 时 content 是空串，
    # 看着像"模型不想说话"，其实是没留给正文的钱。判定类短任务尤其容易撞上。
    budget = int(max_tokens or d.get("max_tokens", 800))
    last = None
    for attempt in range(3):
        body = {
            "model": d["model"],
            "messages": messages,
            "temperature": d.get("temperature", 1.0),
            "max_tokens": budget,
            "stream": False,
        }
        # 思考链越长，回答越像在"写报告"。聊天场景压到 low，句子会短很多。
        effort = d.get("reasoning_effort")
        if effort:
            body["reasoning_effort"] = effort
        req = urllib.request.Request(
            d["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + d["api_key"],
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=d.get("timeout", 60)) as r:
                payload = json.load(r)
            choice = payload["choices"][0]
            text = (choice["message"].get("content") or "").strip()
            if text:
                return text
            if choice.get("finish_reason") == "length" and budget < 1200:
                budget *= 3
                log.warning("正文被思考链占满，max_tokens 提到 %d 重试", budget)
                continue
            return ""
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("DeepSeek 第 %d 次失败: %s", attempt + 1, exc)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"DeepSeek 调用失败: {last}")

def deepseek_tool_chat(cfg: Dict[str, Any], messages: List[Dict[str, Any]],
                       tools: List[Dict[str, Any]],
                       max_tokens: Optional[int] = None) -> Dict[str, Any]:
    """带工具的一轮对话。返回 {"content": str, "tool_calls": [...]}。

    和 deepseek_chat 的关键区别：**模型决定调工具时 content 本来就是空的**，
    这不是"没生成出来"，绝不能当成异常去翻倍重试 —— 否则会重复申请同一个工具。
    """
    d = cfg["deepseek"]
    budget = int(max_tokens or d.get("max_tokens", 320))
    last = None
    for attempt in range(3):
        body = {
            "model": d["model"],
            "messages": messages,
            "temperature": d.get("temperature", 1.0),
            "max_tokens": budget,
            "stream": False,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        effort = d.get("reasoning_effort")
        if effort:
            body["reasoning_effort"] = effort
        req = urllib.request.Request(
            d["base_url"].rstrip("/") + "/chat/completions",
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + d["api_key"],
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=d.get("timeout", 60)) as r:
                payload = json.load(r)
            choice = payload["choices"][0]
            msg = choice.get("message") or {}
            calls = msg.get("tool_calls") or []
            text = (msg.get("content") or "").strip()
            if calls:
                clean = []
                for c in calls:
                    fn = c.get("function") or {}
                    clean.append({
                        "id": c.get("id") or ("call_" + uuid.uuid4().hex[:12]),
                        "type": "function",
                        "function": {"name": fn.get("name") or "",
                                     "arguments": fn.get("arguments") or "{}"},
                    })
                return {"content": text, "tool_calls": clean}
            if text:
                return {"content": text, "tool_calls": []}
            if choice.get("finish_reason") == "length" and budget < 1200:
                budget *= 3
                log.warning("带工具的这一轮正文被思考链占满，max_tokens 提到 %d 重试", budget)
                continue
            return {"content": "", "tool_calls": []}
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("DeepSeek(工具) 第 %d 次失败: %s", attempt + 1, exc)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"DeepSeek 调用失败: {last}")

# ---------------------------------------------------------------- 清理回复文本

MD_PAT = re.compile(r"[*_`#>]+")
# 模型想联网时约定只回一行 "SEARCH: 关键词"，这里负责识别
SEARCH_RE = re.compile(r"SEARCH\s*[:：]\s*(.+)", re.I)
# 模型想发表情包时单独占一行：STICKER: 无语。这一行永远不发给用户，只用来挑图
STICKER_RE = re.compile(r"(?:^|\n)\s*STICKER\s*[:：]\s*([^\n]{1,12})", re.I)
SENT_RE = re.compile(r"[^。！？!?～~]+[。！？!?～~]*")
END_PUNCT = ("。", "！", "？", "!", "?", "～", "~", "…")


def clean_reply(text: str, max_chars: int, max_sentences: int = 2) -> str:
    """把模型的长回答压成微信聊天的样子：最多 N 句、不超过 max_chars 字。

    模型被要求写短但经常不听话，所以这里做硬约束。先按句号切，超出的句子直接丢掉，
    而不是从中间截断——截断会留下半句话，读起来更像机器。
    """
    text = text.replace("\r", "")
    text = re.sub(r"^[\s\-•]+", "", text, flags=re.M)
    text = MD_PAT.sub("", text)
    # 微信里换行会显得像写公文，压成一行；句号后的换行直接去掉，其余当停顿
    text = re.sub(r"(?<=[。！？!?～~…])\s*\n+\s*", "", text)
    text = re.sub(r"\s*\n+\s*", "，", text)
    text = text.strip().strip("，、；,;")

    sents = [s for s in SENT_RE.findall(text) if s.strip()]
    if len(sents) > max_sentences:
        text = "".join(sents[:max_sentences]).rstrip().rstrip("，、；,;")
        if not text.endswith(END_PUNCT):
            text += "…"

    if len(text) > max_chars:
        cut = text[:max_chars]
        idx = max(cut.rfind("，"), cut.rfind("、"), cut.rfind("；"))
        if idx >= max_chars * 0.55:
            cut = cut[:idx]
        text = cut.rstrip("，、；,. ") + "…"
    return text

# ---------------------------------------------------------------- 连发分段

BUBBLE_SEP = re.compile(r"\s*[|｜]\s*")


def extract_sticker(text: str) -> Tuple[str, str]:
    """把回复里的 "STICKER: 无语" 拆出来，返回 (正文, 情绪词)。

    这一行是给程序的指令，绝不能出现在聊天里。关键词只取中文/字母，去掉标点，
    免得模型写成 "STICKER: 无语（笑）" 导致匹配不上。
    """
    kw = ""
    m = STICKER_RE.search(text or "")
    if m:
        # 只取第一段：模型偶尔写 "无语（笑）"，取"无语"就好，不要拼成一长串
        parts = re.split(r"[^\w\u4e00-\u9fff]+", m.group(1).strip())
        kw = (parts[0] if parts else "")[:12]
        text = STICKER_RE.sub("", text, count=1)
    text = re.sub(r"\n{2,}", "\n", text or "").strip()
    return text, kw


def clipboard_text() -> Optional[str]:
    """读剪贴板里的文本。发图片会让微信把剪贴板换成文件，用完得还回去。"""
    try:
        import win32clipboard as cb

        cb.OpenClipboard()
        try:
            if cb.IsClipboardFormatAvailable(cb.CF_UNICODETEXT):
                return cb.GetClipboardData(cb.CF_UNICODETEXT)
        finally:
            cb.CloseClipboard()
    except Exception:  # noqa: BLE001
        pass
    return None


def clipboard_set_text(text: Optional[str]) -> None:
    if not text:
        return
    try:
        import win32clipboard as cb

        cb.OpenClipboard()
        try:
            cb.EmptyClipboard()
            cb.SetClipboardData(cb.CF_UNICODETEXT, text)
        finally:
            cb.CloseClipboard()
    except Exception:  # noqa: BLE001
        pass


def split_bubbles(text: str, max_bubbles: int = 2, min_len: int = 4) -> List[str]:
    """把一条回复拆成最多 N 条短消息。

    优先认模型自己给的 | 分隔符（它知道哪里该断）；没用分隔符就按句末标点看，
    但只在每句都够长时才拆 —— "好。""嗯"这种拆开发反而像机器人在抖机灵。
    """
    text = (text or "").strip()
    if not text:
        return []

    parts = [p.strip() for p in BUBBLE_SEP.split(text) if p.strip()]
    if len(parts) < 2:
        sents = [s.strip() for s in SENT_RE.findall(text) if s.strip()]
        if len(sents) >= 2 and all(len(s) >= min_len for s in sents[:max_bubbles]):
            parts = sents

    # 模型只给了一整句、且偏长时，在逗号处断开 —— 否则会出现"一坨逗号"的长消息
    if len(parts) < 2 and len(text) >= 16:
        m = re.match(r"^(.{4,}?)[，,、]\s*(.{4,})$", text)
        if m:
            parts = [m.group(1), m.group(2)]

    if len(parts) < 2:
        return [text]

    head = parts[:max_bubbles - 1]
    tail = "".join(parts[max_bubbles - 1:])
    return [p for p in head + [tail] if p]


# ---------------------------------------------------------------- 节奏
#
# 真人聊天的两条铁律：① 你还在打字的时候对方不会插话，等你说完才接；
# ② 看到消息不会 0.2 秒就吐字，中间有一段"想"的时间。这两件事做不对，
# 无论句子写得多像人，节奏一出就是机器。

# 只有这些字组成的一句话才算"没什么可接的"（嗯、哦、好、哈哈…）。
# 用白名单而不是黑名单：宁可多回一句，也不要把「在吗」「还行」这种漏判成不用理。
LOW_CONTENT_CHARS = set(
    "嗯哦噢喔呃啊额诶欸好行对是收到了解明白晓哈嘿呵嘻谢啦吧呢嘛呀么的"
    "okOKnMm，。！？、…~～!?,."
)

# 带这些字的一律当"在等回应"，绝不静默
ASK_CHARS = tuple("？?吗怎为几多少哪啥谁")


def is_low_content(text: str) -> bool:
    """这条消息是不是纯应答/语气词，接不接都不影响对话。"""
    t = (text or "").strip()
    if not t:
        return True
    if len(t) > 8:
        return False
    if any(c in t for c in ASK_CHARS):
        return False
    return all(c in LOW_CONTENT_CHARS for c in t)


def _pair(value: Any, default: Tuple[float, float]) -> Tuple[float, float]:
    try:
        lo, hi = float(value[0]), float(value[1])
    except (TypeError, IndexError, ValueError, KeyError):
        return default
    if hi < lo:
        return hi, lo
    return lo, hi


def settle_delay(pcfg: Dict[str, Any], n_msgs: int) -> float:
    """最后一条之后还要安静多久，才认定"他说完了"。

    说得越多，越可能没说完，所以条数往上加一点。这个值会被缓存到这一轮结束，
    不能每轮重抽——否则等待时间会随机跳动，反而更假。
    """
    lo, hi = _pair(pcfg.get("settle_seconds"), (5.0, 10.0))
    per = float(pcfg.get("settle_per_extra", 2.0) or 0)
    cap = float(pcfg.get("max_settle_seconds", 60) or 60)
    return min(cap, random.uniform(lo, hi) + per * max(0, n_msgs - 1))


def think_delay(pcfg: Dict[str, Any], text: str) -> float:
    """从"看到"到"开始回"的思考时间，长一点的问题想得久一点。"""
    lo, hi = _pair(pcfg.get("think_seconds"), (1.5, 4.0))
    per = float(pcfg.get("think_per_char", 0.05) or 0)
    cap = float(pcfg.get("think_max", 7.0) or 7.0)
    return min(cap, random.uniform(lo, hi) + per * len(text or ""))


# ---------------------------------------------------------------- 时间窗

HM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_hm(text: str, default: int) -> int:
    """'08:00' -> 480。格式不对就用默认值，不让配置写错把机器人搞挂。"""
    m = HM_RE.match((text or "").strip())
    if not m:
        return default
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return default
    return h * 60 + mi


def in_window(now_min: int, start: int, end: int) -> bool:
    """左闭右开。start > end 表示跨午夜（23:00 -> 08:00）。"""
    if start == end:
        return False
    if start < end:
        return start <= now_min < end
    return now_min >= start or now_min < end


def in_quiet_hours(now_min: int, quiet: list) -> bool:
    try:
        start = parse_hm(quiet[0], 23 * 60)
        end = parse_hm(quiet[1], 8 * 60)
    except (TypeError, IndexError):
        return False
    return in_window(now_min, start, end)


# ---------------------------------------------------------------- 主流程

class Bridge:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.state = load_state()
        self.behavior = cfg["behavior"]
        self.sent_times: Dict[str, float] = {}
        self.send_log: List[float] = []
        self.web_times: List[float] = []
        self._target_cache: Dict[str, str] = {}
        self.persona = ""
        self.persona_mtime = 0.0
        self.cfg_mtime = 0.0
        try:
            self.cfg_mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            pass
        self.kb = None
        self.db = None
        self.gui = None
        self.account = None
        self.account_nickname = ""
        self.hwnd = None
        self.self_username = None
        self._last_proactive_check = 0.0
        self._last_proactive_why = ""
        self.mem = None
        self.curio = None
        self._learning = False
        self._last_learn_check = 0.0
        self._last_learn_why = ""
        self.stickers = None
        self.inbox = None
        self.media = None
        self._pending_sticker = ""
        self._last_sticker_at = 0.0
        self._collecting = False
        self._last_collect_check = 0.0
        self._last_collect_why = ""
        self.paused = False
        self._mem_lock = threading.Lock()
        # 还没到"他说完了"的消息缓冲：{user: {texts, first_at, last_at, wait, n}}
        self.pending: Dict[str, Dict[str, Any]] = {}
        # 连续"不接话"的次数，用来防止一直不回把人晾着
        self._silence_streak: Dict[str, int] = {}
        self._answering = False
        # -- 看电脑（只读）。她申请、人批准、她再接着说话，中间不阻塞主循环
        self.desk = None
        # user -> {id, calls, messages, nick, created, rounds, timeout}
        self.desk_requests: Dict[str, Dict[str, Any]] = {}
        # user -> {req_id, outputs, allowed}
        self.desk_results: Dict[str, Dict[str, Any]] = {}
        self.desk_log: List[Dict[str, Any]] = []

    # -- 人设与知识库
    def load_persona(self, force: bool = False) -> None:
        path = BASE / self.cfg.get("persona_file", "persona.md")
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        if not force and mtime == self.persona_mtime and self.persona:
            return
        if mtime:
            self.persona = path.read_text(encoding="utf-8").strip()
            self.persona_mtime = mtime
            log.info("已加载人设卡 %s（%d 字）", path.name, len(self.persona))
        else:
            self.persona = self.cfg.get("system_prompt", "").strip()
            log.warning("没找到 %s，回退到 config.system_prompt", path.name)

    def init_knowledge(self) -> None:
        kcfg = self.cfg.get("knowledge") or {}
        if not kcfg.get("enabled"):
            log.info("知识库未启用")
            return
        from knowledge import KnowledgeBase

        self.kb = KnowledgeBase(BASE / kcfg.get("dir", "knowledge"))
        st = self.kb.stats()
        log.info("知识库就绪：%s 个文件 / %s 个片段 / 词表 %s",
                 st["files"], st["chunks"], st["vocab"])
        if not st["chunks"]:
            log.warning("知识库是空的，%s 里还没有可读的 .md/.txt", kcfg.get("dir"))

    def init_memory(self) -> None:
        mcfg = self.cfg.get("memory") or {}
        if not mcfg.get("enabled", True):
            log.info("长期记忆未启用")
            return
        from memory import Memory

        self.mem = Memory(BASE, max_items=int(mcfg.get("max_items", 40)))
        users = self.mem.users()
        log.info("长期记忆就绪：%d 个会话有记忆，上限 %d 条/人",
                 len(users), self.mem.max_items)

    # -- 自学
    def learned_dir(self) -> Path:
        kcfg = self.cfg.get("knowledge") or {}
        name = (self.cfg.get("learn") or {}).get("learned_dir", "learned")
        return BASE / kcfg.get("dir", "knowledge") / name

    def init_curiosity(self) -> None:
        lcfg = self.cfg.get("learn") or {}
        if not lcfg.get("enabled", True):
            log.info("自学未启用")
            return
        from learn import Curiosity

        self.curio = Curiosity(
            BASE / "curiosity.json",
            self.learned_dir(),
            max_pending=int(lcfg.get("queue_max", 40)),
        )
        st = self.curio.stats()
        log.info("好奇心队列就绪：待学 %d 条 / 已学 %d 篇 / 今日已学 %d 条",
                 st["pending"], st["done"], st["today"])

    def _llm(self):
        return lambda msgs, max_tokens=700: deepseek_chat(self.cfg, msgs, max_tokens=max_tokens)

    def learn_async(self, user_text: str, replies: List[str]) -> None:
        """回复发出后，顺手判断这轮有没有"她不懂、值得查"的东西，入队等空闲时学。"""
        lcfg = self.cfg.get("learn") or {}
        if not lcfg.get("enabled", True) or self.curio is None:
            return
        if not self.web_enabled():
            return
        if len((user_text or "").strip()) < 4:
            return

        def work() -> None:
            try:
                from learn import extract_curiosity

                items = extract_curiosity(self._llm(), user_text, " ".join(replies))
                added = self.curio.add(items, "chat")
                if added:
                    log.info("记下想查的：%s", "、".join(added))
            except Exception:  # noqa: BLE001
                log.warning("好奇提取失败（不影响回复）: %s", traceback.format_exc(limit=2))

        threading.Thread(target=work, daemon=True).start()

    def maybe_learn(self) -> None:
        """空闲时自己学一个。条件不满足就什么也不做，只在原因变化时记一行。"""
        lcfg = self.cfg.get("learn") or {}
        if not lcfg.get("enabled", True) or self.curio is None:
            return
        if self._learning or self.paused:
            return
        now_ts = time.time()
        if now_ts - self._last_learn_check < 120:
            return
        self._last_learn_check = now_ts

        def skip(why: str) -> None:
            if why != self._last_learn_why:
                self._last_learn_why = why
                log.info("暂不自学：%s", why)

        now = datetime.now()
        start = parse_hm(lcfg.get("start", "09:00"), 540)
        end = parse_hm(lcfg.get("end", "23:00"), 1380)
        if not in_window(now.hour * 60 + now.minute, start, end):
            return skip("不在 %s-%s 时段" % (lcfg.get("start", "09:00"), lcfg.get("end", "23:00")))
        cap = int(lcfg.get("max_per_day", 5))
        if self.curio.today_count() >= cap:
            return skip("今天已经学了 %d 条，到上限了" % cap)
        gap = float(lcfg.get("min_gap_minutes", 20)) * 60
        wait = gap - (now_ts - self.curio.last_learn_at())
        if wait > 0:
            return skip("距上次自学还有 %d 分钟冷却" % max(1, int(wait / 60)))

        user = self._proactive_target()
        if user:
            try:
                last_t, _, _ = self._chat_stats(user)
            except Exception:  # noqa: BLE001
                last_t = 0.0
            idle = float(lcfg.get("idle_minutes", 10)) * 60
            if last_t and now_ts - last_t < idle:
                return skip("刚聊过 %d 分钟，先把注意力留给他" % int((now_ts - last_t) / 60))

        topic = self.curio.next_pending()
        if topic is None:
            revived = self.curio.revive()
            if revived:
                log.info("把 %d 个之前没查成的重新排一次队", revived)
                topic = self.curio.next_pending()
        if topic is None and lcfg.get("allow_invent", True):
            from learn import invent_topic

            idea = invent_topic(self._llm(), lcfg.get("interests", "视觉设计、AI 工具、深圳生活"))
            if idea:
                self.curio.add([idea], "curious")
                log.info("自己想了个题：%s", idea["topic"])
                topic = self.curio.next_pending()
        if topic is None:
            return skip("队列空的，暂时也想不出要查什么")

        self._last_learn_why = ""
        self._learning = True
        threading.Thread(target=self._do_learn, args=(topic,), daemon=True).start()
        log.info("开始自学：%s（%s）", topic.get("topic"), topic.get("why") or topic.get("source"))

    def _do_learn(self, topic: Dict[str, Any]) -> None:
        name = topic.get("topic") or ""
        try:
            from learn import learn_one

            res = learn_one(name, self._llm(), self.learned_dir())
            if res.get("ok"):
                self.curio.mark(topic["id"], "done", res.get("summary", ""))
                n = self.curio.bump()
                log.info("自学完成（今日第 %d 条）：%s → %s（%d 字）",
                         n, name, res.get("file"), len(res.get("summary") or ""))
            else:
                self.curio.mark(topic["id"], "skipped", res.get("reason", ""))
                log.info("自学放弃：%s（%s）", name, res.get("reason"))
        except Exception:  # noqa: BLE001
            log.error("自学异常:\n%s", traceback.format_exc())
            try:
                self.curio.mark(topic["id"], "skipped", "异常")
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._learning = False

    def learn_status(self) -> Dict[str, Any]:
        if self.curio is None:
            return {"enabled": False, "pending": [], "notes": [], "stats": {}}
        from learn import learned_notes

        items = self.curio.items()
        return {
            "enabled": True,
            "learning": self._learning,
            "stats": self.curio.stats(),
            "pending": [
                {"id": x["id"], "topic": x.get("topic", ""), "why": x.get("why", ""),
                 "source": x.get("source", ""), "tries": x.get("tries", 0)}
                for x in items if x.get("status") == "pending"
            ],
            "notes": learned_notes(self.learned_dir()),
        }

    def learn_now(self, topic: str = "") -> Dict[str, Any]:
        """控制台手动触发。可以带一个话题，也可以让她从队列里挑。"""
        if self.curio is None:
            return {"ok": False, "reason": "自学未启用"}
        if self._learning:
            return {"ok": False, "reason": "她正在学一个，等它结束"}
        if topic.strip():
            self.curio.add([{"topic": topic.strip()[:24], "why": "手动指定"}], "manual")
        item = self.curio.next_pending()
        if item is None:
            return {"ok": False, "reason": "队列是空的"}
        self._learning = True
        threading.Thread(target=self._do_learn, args=(item,), daemon=True).start()
        return {"ok": True, "topic": item.get("topic", "")}

    # -- 表情包 ------------------------------------------------------
    def sticker_cfg(self) -> Dict[str, Any]:
        return self.cfg.get("stickers") or {}

    def init_stickers(self) -> None:
        scfg = self.sticker_cfg()
        if not scfg.get("enabled", True):
            log.info("表情包未启用")
            return
        from stickers import StickerBook

        self.stickers = StickerBook(
            BASE / str(scfg.get("dir") or "stickers"),
            max_items=int(scfg.get("max_items", 160)),
        )
        st = self.stickers.stats()
        log.info("表情包就绪：%d 张，%.1f MB，标签 %s",
                 st["count"], st["size_mb"], list(st["tags"])[:6] or "无")

    def init_inbox(self) -> None:
        """他发来的图片、表情包、语音、文件 —— 以前这些在过滤那一行就被扔了。"""
        icfg = self.cfg.get("inbox") or {}
        if not icfg.get("enabled", True):
            log.info("非文本消息识别未启用（发图、发表情包她不会有反应）")
            return
        from inbox import Inbox

        self.inbox = Inbox(
            BASE / str(icfg.get("dir") or "inbox"),
            stickers=self.stickers,
            media=self.media,
            db=self.db,
            max_items=int(icfg.get("max_items", 60)),
        )
        st = self.inbox.status()
        log.info("收件箱就绪：留着最近 %d 条，已认识 %d 张表情",
                 st["count"], st["known"])

    # -- 看电脑（只读，每次调用都要人批准）
    def desk_cfg(self) -> Dict[str, Any]:
        return self.cfg.get("desk") or {}

    def init_desk(self) -> None:
        dcfg = self.desk_cfg()
        if not dcfg.get("enabled", True):
            log.info("看电脑的能力未启用")
            return
        self.desk = Desk(BASE, dcfg)
        roots = "、".join(r.name for r in self.desk.allow_roots)
        log.info("看电脑就绪（只读，每次要批准）：可看 %s", roots)

    def _desk_tools(self) -> List[Dict[str, Any]]:
        if self.desk is None:
            return []
        want = self.desk_cfg().get("tools") or []
        return build_tools_spec(want or None)

    def desk_status(self) -> Dict[str, Any]:
        """给控制台：待批准的请求、操作流水、能不能看。"""
        if self.desk is None:
            return {"enabled": False, "pending": [], "history": [], "pending_count": 0}
        pend = []
        now = time.time()
        for user, req in self.desk_requests.items():
            if req.get("done"):
                continue          # 已经批过、等她接着说，别再显示成"待批"
            left = max(0, int(req.get("timeout", 300) - (now - req["created"])))
            pend.append({
                "id": req["id"],
                "user": user,
                "nick": req.get("nick") or user,
                "left": left,
                "rounds": req.get("rounds", 1),
                "calls": req["calls"],
            })
        hist = list(reversed(self.desk.history[:40])) if self.desk else []
        return {
            "enabled": True,
            "pending": pend,
            "pending_count": len(pend),
            "history": hist,
            "shots": [*self._desk_shots()],
            "roots": [str(r) for r in self.desk.allow_roots],
        }

    def _desk_shots(self) -> List[str]:
        if self.desk is None:
            return []
        try:
            files = sorted(self.desk.shot_dir.glob("shot_*.png"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            return [f.name for f in files[:6]]
        except OSError:
            return []

    def _request_desk(self, user: str, nick: str, messages: List[Dict[str, Any]],
                      calls: List[Dict[str, Any]], rounds: int = 1,
                      user_text: str = "") -> List[str]:
        """她申请看电脑。**不执行**，等人在控制台点允许。"""
        items = []
        for c in calls:
            fn = c.get("function") or {}
            name = fn.get("name") or ""
            try:
                args = json.loads(fn.get("arguments") or "{}")
                if not isinstance(args, dict):
                    args = {}
            except json.JSONDecodeError:
                args = {}
            items.append({
                "id": c["id"],
                "tool": name,
                "args": args,
                "preview": self.desk.preview(name, args),
            })

        timeout = int(self.desk_cfg().get("approve_timeout", 300))
        self.desk_requests[user] = {
            "id": uuid.uuid4().hex[:10],
            "calls": items,
            "messages": messages,
            "nick": nick,
            "created": time.time(),
            "rounds": rounds,
            "timeout": timeout,
            "user_text": user_text or (messages[-1].get("content") if messages else ""),
        }
        self.desk_log.append({"at": time.strftime("%H:%M:%S"), "kind": "申请",
                              "who": nick, "text": "；".join(i["preview"] for i in items)})
        log.info("她申请看电脑（%s）：%s —— 等你在控制台点允许",
                 nick, "；".join(i["preview"] for i in items))

        # 这句是程序替她说的话（她这轮还没生成正文），所以给几个说法换着来
        if rounds > 1:
            lead = random.choice(["刚才那个不够，我再翻一个地方", "还得再看一处"])
            return [lead + "（再点一次允许）"]
        lead = random.choice(["等我看一眼", "我去看下", "让我翻一下"])
        return [lead + "（去控制台点个允许）"]

    def approve_desk(self, user: str, req_id: str, allow: bool) -> Dict[str, Any]:
        """控制台点了允许/拒绝。执行完把结果排队，主循环会让她接着说。"""
        req = self.desk_requests.get(user)
        if not req or req["id"] != req_id:
            return {"ok": False, "error": "这个请求已经过期或处理过了"}
        if req.get("done"):
            # 双击、或者网络慢手抖点两下 —— 不挡住的话同一个工具会执行两遍
            return {"ok": False, "error": "这次已经批过了"}
        req["done"] = True

        outputs: List[Dict[str, Any]] = []
        if not allow:
            for c in req["calls"]:
                outputs.append({"call_id": c["id"],
                                "content": "对方没允许这次操作，你什么都没看到。"})
            self.desk_log.append({"at": time.strftime("%H:%M:%S"), "kind": "拒绝",
                                  "who": req.get("nick") or user,
                                  "text": "；".join(c["preview"] for c in req["calls"])})
            log.info("你拒绝了 %s 的查看请求", req.get("nick") or user)
        else:
            for c in req["calls"]:
                r = self.desk.execute(c["tool"], c["args"])
                outputs.append({"call_id": c["id"], "content": r.as_tool_message(),
                                "shot": r.shot, "ok": r.ok})
                self.desk_log.append({
                    "at": time.strftime("%H:%M:%S"), "kind": "允许" if r.ok else "失败",
                    "who": req.get("nick") or user,
                    "text": f"{c['preview']} → {r.detail or ('成功' if r.ok else r.text[:30])}",
                    "shot": r.shot,
                })
                log.info("批准执行 %s：%s", c["tool"], r.detail or r.text[:40])

        self.desk_results[user] = {"req_id": req_id, "outputs": outputs, "allowed": allow}
        return {"ok": True}

    def maybe_resume_desk(self) -> None:
        """批准之后，让她接着说 —— 这是「申请-批准-继续」的第三段。

        不能同步等批准，否则微信轮询会停在那儿，你的消息要等半天才被读到。
        """
        now = time.time()
        # 超时没批的，别一直挂着
        for user, req in list(self.desk_requests.items()):
            if user in self.desk_results:
                continue
            if now - req["created"] > req.get("timeout", 300):
                self.desk_requests.pop(user, None)
                self.desk_log.append({"at": time.strftime("%H:%M:%S"), "kind": "超时",
                                      "who": req.get("nick") or user,
                                      "text": "等太久没批，放弃了"})
                log.info("等批准超时（%s），放弃这次", req.get("nick") or user)
                try:
                    nick = req.get("nick") or user
                    self.send(user, "算了，不看了", nick)
                except Exception:  # noqa: BLE001
                    log.error("超时提示发送失败:\n%s", traceback.format_exc())

        for user, res in list(self.desk_results.items()):
            req = self.desk_requests.pop(user, None)
            self.desk_results.pop(user, None)
            if not req or req["id"] != res["req_id"]:
                continue
            try:
                self._finish_desk(user, req, res)
            except Exception:  # noqa: BLE001
                log.error("继续处理看电脑结果失败:\n%s", traceback.format_exc())

    def _finish_desk(self, user: str, req: Dict[str, Any], res: Dict[str, Any]) -> None:
        nick = req.get("nick") or self.db.get_nickname(user) or user
        messages = list(req["messages"])
        messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": c["id"], "type": "function",
                            "function": {"name": c["tool"],
                                         "arguments": json.dumps(c["args"], ensure_ascii=False)}}
                           for c in req["calls"]],
        })
        for out in res["outputs"]:
            messages.append({"role": "tool", "tool_call_id": out["call_id"],
                             "content": out["content"]})

        max_rounds = int(self.desk_cfg().get("max_rounds", 3))
        rounds = req.get("rounds", 1)
        raw = ""
        for _ in range(3):
            got = deepseek_tool_chat(self.cfg, messages, self._desk_tools(), max_tokens=420)
            if got["tool_calls"]:
                if rounds >= max_rounds:
                    log.warning("工具轮数到上限（%d），不再批新的", max_rounds)
                    messages.append({"role": "user",
                                     "content": "（系统：这次不能再看了，直接说你看到的就行）"})
                    continue
                # 又想看别的 —— 再走一次批准
                bubbles = self._request_desk(user, nick, messages, got["tool_calls"],
                                             rounds + 1, str(req.get("user_text") or ""))
                self._deliver(user, nick, bubbles, [], "")
                return
            raw = got["content"]
            break

        raw, sticker = extract_sticker(raw or "")
        bubbles = self.compose_bubbles(raw)
        if not bubbles and not sticker:
            log.info("看完电脑她没话可说")
            return
        self._deliver(user, nick, bubbles, [str(req.get("user_text") or "")].copy()
                      if req.get("user_text") else [], sticker)

    def _deliver(self, user: str, nick: str, bubbles: List[str],
                 texts: List[str], sticker: str) -> bool:
        """把最终要说的话发出去，并写进历史。看电脑那轮的收尾共用这里。"""
        sent = False
        if bubbles:
            sent = self.send_bubbles(user, bubbles, nick)
        if sent:
            self.sent_times[user] = time.time()
            self.send_log.append(time.time())
            log.info("已回复 %s（%d 条，看电脑后续）: %s", nick, len(bubbles), " ／ ".join(bubbles))
            hist = self.state["history"].get(user, [])
            if texts:
                hist.append({"role": "user", "content": "\n".join(texts)})
            for b in bubbles:
                hist.append({"role": "assistant", "content": b})
            keep = self.behavior.get("history_turns", 8) * 2
            self.state["history"][user] = hist[-keep:]
            save_state(self.state)
        if sticker and (sent or not bubbles):
            self.send_sticker(user, nick, sticker)
        return sent

    def send_sticker(self, user: str, nick: str, keyword: str) -> bool:
        """按情绪词挑一张发出去。挑不到就不发 —— 宁可这次没有，也别发错情绪的图。"""
        if self.stickers is None:
            return False
        scfg = self.sticker_cfg()
        now = time.time()
        gap = float(scfg.get("min_gap_minutes", 20)) * 60
        if self._last_sticker_at and now - self._last_sticker_at < gap:
            log.info("跳过表情包：距上一条 %.0f 分钟", (now - self._last_sticker_at) / 60)
            return False

        item = self.stickers.pick(keyword)
        if not item:
            self.stickers.want(keyword)
            log.info("仓库里没有「%s」的表情包，已记下，晚点自己去收", keyword)
            return False
        path = self.stickers.path_of(item["file"])
        if not path:
            return False

        target = self.resolve_target(user, nick)
        saved = clipboard_text()          # 发图会占用剪贴板，先存下你的内容
        try:
            resp = self.gui.send_image(str(path), who=target, verify=False)
            log.info("send_image(%s) 返回: %s", target, resp)
            ok = not (isinstance(resp, dict) and resp.get("status") == "失败")
            if ok:
                self._last_sticker_at = time.time()
                self.stickers.mark_used(item)
                log.info("已发表情包「%s」→ %s（%dKB）", keyword, item["file"],
                         int(item.get("bytes", 0) / 1024))
            return ok
        except Exception as exc:  # noqa: BLE001
            log.warning("表情包发送失败: %s", exc)
            return False
        finally:
            clipboard_set_text(saved)

    def sticker_status(self) -> Dict[str, Any]:
        if self.stickers is None:
            return {"enabled": False, "items": [], "stats": {}}
        st = self.stickers.stats()
        items = [{
            "file": it.get("file"),
            "tags": it.get("tags") or [],
            "uses": it.get("uses") or 0,
            "added_at": it.get("added_at", ""),
            "kb": int((it.get("bytes") or 0) / 1024),
        } for it in self.stickers.all()]
        items.sort(key=lambda x: x.get("added_at") or "", reverse=True)
        # 不截断：库上限 160，截了的话老图在控制台上看不见也删不掉
        return {"enabled": True, "collecting": self._collecting,
                "stats": st, "items": items}

    def _next_collect_keyword(self) -> str:
        """收集顺序：缺的情绪优先（聊天时想发却没有），其次按兴趣轮着来。"""
        scfg = self.sticker_cfg()
        want = self.stickers.pop_want() if self.stickers else None
        if want:
            return want
        emotions = [e.strip() for e in (scfg.get("emotions") or []) if e.strip()]
        counts = (self.stickers.stats().get("tags") or {}) if self.stickers else {}
        for e in emotions:
            if counts.get(e, 0) < 2:
                return e
        pool = emotions + [x.strip() for x in
                           re.split(r"[、,，;；\s]+", scfg.get("interests") or "")
                           if x.strip()]
        if not pool:
            return ""
        # 挑当前数量最少的那个（缺什么补什么）；同数量时按 idx 轮换，避免总是同一个
        meta = self.state.setdefault("stickers_meta", {})
        idx = int(meta.get("idx") or 0)
        rot = pool[idx % len(pool):] + pool[:idx % len(pool)]
        kw = min(rot, key=lambda k: counts.get(k, 0))
        meta["idx"] = (idx + 1) % len(pool)
        save_state(self.state)
        return kw

    def maybe_collect_stickers(self) -> None:
        """空闲时自己攒表情包。5 分钟才查一次，避免每轮都算时间窗。"""
        scfg = self.sticker_cfg()
        if self.stickers is None or not scfg.get("collect_enabled", True):
            return
        if self._collecting:
            return
        now_ts = time.time()
        if now_ts - self._last_collect_check < 300:
            return
        self._last_collect_check = now_ts

        now = datetime.now()
        now_min = now.hour * 60 + now.minute
        lo = parse_hm(scfg.get("collect_start"), 9 * 60 + 30)
        hi = parse_hm(scfg.get("collect_end"), 23 * 60)
        if not in_window(now_min, lo, hi):
            return
        if self.stickers.stats()["count"] >= int(scfg.get("max_items", 160)):
            return
        last = float((self.state.get("stickers_meta") or {}).get("last_collect_ts") or 0)
        if now_ts - last < float(scfg.get("collect_interval_hours", 2)) * 3600:
            return

        kw = self._next_collect_keyword()
        if not kw:
            return
        self._collecting = True
        threading.Thread(target=self._do_collect, args=(kw,), daemon=True).start()

    def _do_collect(self, keyword: str) -> None:
        try:
            n = int(self.sticker_cfg().get("collect_count", 3))
            res = self.stickers.add_query(keyword, count=n)
            if res.get("added"):
                meta = self.state.setdefault("stickers_meta", {})
                meta["last_collect_ts"] = time.time()
                save_state(self.state)
            log.info("收表情包「%s」：新增 %d 张（候选 %d）",
                     res.get("query"), res.get("added", 0), res.get("tried", 0))
        except Exception:  # noqa: BLE001
            log.error("收表情包异常:\n%s", traceback.format_exc())
        finally:
            self._collecting = False

    def collect_now(self, keyword: str = "") -> Dict[str, Any]:
        """控制台手动触发一次收集。"""
        if self.stickers is None:
            return {"ok": False, "reason": "表情包未启用"}
        if self._collecting:
            return {"ok": False, "reason": "正在收一个，等它结束"}
        kw = keyword.strip() or self._next_collect_keyword()
        if not kw:
            return {"ok": False, "reason": "没有可收的关键词"}
        self._collecting = True
        threading.Thread(target=self._do_collect, args=(kw,), daemon=True).start()
        return {"ok": True, "keyword": kw}

    def build_system_prompt(self, query: str, user: Optional[str] = None) -> str:
        self.load_persona()
        parts = [self.persona or self.cfg.get("system_prompt", "")]
        if self.kb is not None:
            kcfg = self.cfg["knowledge"]
            self.kb.maybe_reload()
            ctx = self.kb.context_for(
                query,
                top_k=kcfg.get("top_k", 3),
                min_score=kcfg.get("min_score", 2.5),
                max_chars=kcfg.get("max_chars", 900),
            )
            if ctx:
                log.info("命中知识库：\n%s", ctx[:300].replace("\n", " | "))
                parts.append(
                    "## 你掌握的资料\n"
                    "下面是你知道的事实，需要时自然地说出来。"
                    "不要照抄原文，不要提「资料」「知识库」「根据记录」这类词。"
                    "资料里没有的就说不知道。\n\n" + ctx
                )
            else:
                log.info("知识库未命中，按闲聊处理")
        if self.mem is not None and user:
            mcfg = self.cfg.get("memory") or {}
            try:
                mem_ctx = self.mem.context_for(
                    user, query, limit=int(mcfg.get("inject_limit", 6)))
            except Exception as exc:  # noqa: BLE001
                mem_ctx = ""
                log.warning("读取长期记忆失败: %s", exc)
            if mem_ctx:
                parts.append(
                    "## 你记得他的事\n"
                    "这些是你以前跟他聊天时攒下的印象，需要时自然带出来，"
                    "别列清单、别说「我记得你说过」。\n\n" + mem_ctx
                )
        if self.web_enabled():
            parts.append(
                "## 联网\n"
                "你看不到实时信息（天气、新闻、比分、票价、最近发生的事）。"
                "遇到这类问题先查一下：**整条回复只输出一行**\n"
                "SEARCH: 搜索关键词\n"
                "其余情况正常聊天，绝对不要出现 SEARCH 这三个字母。"
            )
        bcfg = self.cfg.get("burst") or {}
        if bcfg.get("enabled"):
            parts.append(
                "## 连发\n"
                "一句说完就停。确实想补一句时，用 | 隔成两条短消息，"
                "比如「嗯懂了|那你打算啥时候弄」。"
                f"最多 {int(bcfg.get('max_bubbles', 2))} 条，多数情况不用分。"
            )
        scfg = self.sticker_cfg()
        if self.stickers is not None and scfg.get("enabled", True):
            tags = list((self.stickers.stats().get("tags") or {}).keys())
            if tags:
                parts.append(
                    "## 表情包\n"
                    "你手机里存了些表情包，可以配在回复后面发。正文写完后**另起一行**写：\n"
                    "STICKER: 情绪词\n"
                    f"情绪词**只能**从这几个里挑：{'、'.join(tags[:12])}。\n"
                    "挑列表外的词会挑不到图，等于白写，所以别自己造词。\n"
                    "对方带着情绪说话时（累、烦、无语、好笑、尴尬、开心）就配一张；"
                    "纯问事、办正事、在安慰人的时候不配。只写情绪词，不要解释为什么发。"
                )
        # 常驻一条，历史里也会留下方括号，多轮之后她不能把这些念出来
        parts.append(
            "## 方括号\n"
            "消息里用方括号标的是程序给的字（[他发来一张图片]、[他发来一个表情包：无语]），"
            "不是他说的话。别念出来，也别编图里的内容。"
            "表情包后面带情绪说明你认得这一张，可以顺着接；没带就是没认出来。"
        )
        if self._desk_tools():
            parts.append(
                "## 你能看他的电脑\n"
                "你手上有几个小工具，能看他屏幕上的字、翻文件夹、读文本文件、找文件、看电脑状态。\n"
                "**什么时候能用**：他说得清楚是要你看他电脑上的东西（"
                "「帮我看看桌面那个文件」「我那个作品集放哪了」「屏幕上报错写的啥」「看看我现在在干嘛」）。\n"
                "**什么时候别动**：只是聊天、问意见、问知识、情绪上来吐槽的时候。"
                "这些情况直接说话，别去翻他电脑——没人喜欢没被叫就去翻自己文件夹的人。\n"
                "**用之前要有心理准备**：每次调用都要他在控制台点「允许」才执行，"
                "所以你会先回一句「等我看一眼」，那不是最终答复，是真去看。\n"
                "**你只能看，不能改**：没有写文件、删文件、运行程序的能力。"
                "他要你改代码、删东西、装软件，直接说你做不到，别答应。\n"
                "**你看的是字，不是画面**：屏幕是识别出来的文字，图标、照片内容、视频你都不知道。"
                "别描述你没读到的东西，也别假装看见了。\n"
                "**看得少一点**：一次能说清的事别来回翻，最多两三次调用就要给出结论。"
            )
        return "\n\n".join(p for p in parts if p)

    # -- 联网检索
    def web_enabled(self) -> bool:
        return bool((self.cfg.get("web_search") or {}).get("enabled"))

    def web_budget_ok(self) -> bool:
        wcfg = self.cfg.get("web_search") or {}
        limit = int(wcfg.get("max_per_hour", 30))
        now = time.time()
        self.web_times = [t for t in self.web_times if now - t < 3600]
        if len(self.web_times) >= limit:
            log.warning("联网查询已达上限（%d 次/小时），本次跳过", limit)
            return False
        self.web_times.append(now)
        return True

    def _reask(self, messages: List[Dict[str, str]], raw: str, note: str) -> str:
        messages2 = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": note + "一句话，不超过 30 字。"},
        ]
        try:
            return deepseek_chat(self.cfg, messages2)
        except Exception as exc:  # noqa: BLE001
            log.error("二次生成失败: %s", exc)
            return ""

    def maybe_web_answer(self, raw: str, messages: List[Dict[str, str]], question: str) -> str:
        """模型想联网时会只回一行 SEARCH:关键词；这里执行检索后重新生成。"""
        m = SEARCH_RE.search(raw or "")
        if not m or len(raw.strip()) > 80:
            return raw
        query = m.group(1).strip().strip("「」“”\"'【】[]").strip()
        if not query:
            return raw
        query = query[:40]
        if not self.web_budget_ok():
            return self._reask(messages, raw, "别查了，按你知道的回答。")
        try:
            import websearch
        except Exception as exc:  # noqa: BLE001
            log.warning("联网模块不可用，按普通回答处理: %s", exc)
            return self._reask(messages, raw, "查不了，按你知道的回答。")

        wcfg = self.cfg.get("web_search") or {}
        # 天气单独走结构化数据源：搜索引擎摘要里拿不到温度
        wctx = ""
        if re.search(r"天气|气温|温度|下雨|下雪|冷不冷|热不热", query):
            city = websearch.city_from_query(query) or (wcfg.get("default_city") or "")
            w = websearch.weather(city)
            if w:
                wctx = "实时天气数据：" + w + "\n"
                log.info("天气查询：%s", w)

        t0 = time.time()
        results, extra = websearch.search_and_read(
            query,
            top=int(wcfg.get("top", 4)),
            read_top=int(wcfg.get("read_top", 1)),
            max_chars=int(wcfg.get("max_chars", 600)),
            timeout=float(wcfg.get("timeout", 8)),
        )
        log.info("联网查「%s」%.1fs，%d 条结果%s", query, time.time() - t0,
                 len(results), "（含正文）" if extra else "")
        if not results and not wctx:
            return self._reask(messages, raw, "没查到，按你知道的回答，不清楚就说不清楚。")

        ctx = websearch.format_for_prompt(results, extra)
        if wctx:
            ctx = wctx + ("\n搜索结果：\n" + ctx if ctx else "")
        messages2 = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": (
                "（这是刚查到的资料）\n" + ctx +
                "\n\n用上面的资料回答我刚才的问题，一句话，不超过 30 字。"
                "资料里没有明确答案就直说查不到，不要编。不要再输出 SEARCH。"
            )},
        ]
        try:
            return deepseek_chat(self.cfg, messages2)
        except Exception as exc:  # noqa: BLE001
            log.error("联网后二次生成失败: %s", exc)
            return ""

    # -- 初始化：绑定小号窗口
    def bind(self) -> None:
        from wechatauto import WeChatDB, WeChatGUI

        want = self.cfg.get("account")
        winmap = find_account_windows()
        if not winmap:
            raise RuntimeError("没找到可见的微信主窗口（微信是否已登录、是否最小化？）")

        log.info("检测到已登录微信窗口: %s", {k: v for k, v in winmap.items()})
        if want and want in winmap:
            self.account = want
        elif len(winmap) == 1:
            self.account = next(iter(winmap))
            log.warning("config 指定账号未在窗口中发现，自动使用唯一窗口账号 %s", self.account)
        else:
            raise RuntimeError(f"多个微信窗口且 config.account={want} 不在其中，拒绝启动以免发错人: {winmap}")

        self.hwnd = winmap[self.account]
        self.db = WeChatDB(account=self.account)
        info = self.db.get_self_info()
        self.self_username = info.get("username")
        nick = info.get("nick_name") or self.cfg.get("account_nickname")
        self.account_nickname = nick or ""
        log.info("已绑定账号: %s (%s)  hwnd=%s", nick, self.self_username, self.hwnd)

        self.gui = WeChatGUI(hwnd=self.hwnd)
        if not self.gui.is_alive():
            raise RuntimeError("微信窗口绑定失败")

        # 图片消息只能本地解密（不驱动界面），失败也不影响回别的
        try:
            from wechatauto import MediaDownloader
            self.media = MediaDownloader(self.db)
        except Exception as exc:  # noqa: BLE001
            log.warning("图片下载器初始化失败，图片只留描述：%s", exc)
            self.media = None

    # -- 每轮扫描
    def poll_once(self) -> None:
        if self.paused:
            return
        sessions = self.db.get_sessions(limit=80)
        for s in sessions:
            user = s.get("username") or ""
            if not is_replyable(user):
                continue
            if self.behavior.get("reply_only_whitelist"):
                wl = set(self.behavior.get("whitelist") or [])
                if wl and user not in wl:
                    continue
            try:
                self.handle_chat(user)
            except Exception:
                log.error("处理会话 %s 出错:\n%s", user, traceback.format_exc())

    def handle_chat(self, user: str) -> None:
        msgs = self.db.get_messages(user, limit=20)
        if not msgs:
            return
        wm = self.state["watermarks"].get(user, 0)

        # 首次见到这个会话：只记录水位，不回复历史消息
        if user not in self.state["watermarks"]:
            self.state["watermarks"][user] = max(m.get("sort_seq", 0) for m in msgs)
            save_state(self.state)
            log.info("初始化水位 %s -> %s", user, self.state["watermarks"][user])
            return

        incoming = [
            m for m in msgs
            if m.get("sort_seq", 0) > wm
            and m.get("sender_username") == user
        ]
        if not incoming:
            return

        self.state["watermarks"][user] = max(m.get("sort_seq", 0) for m in msgs)
        save_state(self.state)

        # 读到就记下，但不马上回。人在微信里经常一句话拆成几条发，
        # 收到第一条就抢答等于打断他。先攒着，等安静下来再一起说。
        now = time.time()
        item = self.pending.setdefault(
            user, {"texts": [], "parts": [], "first_at": now, "last_at": now,
                   "wait": None, "n": 0}
        )
        if not item.get("parts"):
            item["first_at"] = now
        item.setdefault("parts", [])

        nick = self.db.get_nickname(user) or user
        texts: List[str] = []
        events: List[str] = []
        for m in incoming:
            if m.get("type") == "文本":
                t = (m.get("content") or "").strip()
                if t:
                    texts.append(t)
                    item["parts"].append(t)
                continue
            # 图片 / 表情包 / 语音 / 文件：她本来完全看不见这些
            try:
                line = self.inbox.accept(m, user) if self.inbox is not None else None
            except Exception:  # noqa: BLE001
                log.error("解析非文本消息出错:\n%s", traceback.format_exc())
                line = None
            if line:
                events.append(line)
                item["parts"].append(line)

        if not item["parts"]:
            return
        item["texts"].extend(texts)
        item["last_at"] = now

        if texts:
            log.info("收到 %s(%s): %s  ← 先等他说完（已攒 %d 条）",
                     nick, user, " / ".join(texts), len(item["texts"]))
        if events:
            log.info("收到 %s(%s) 的非文本消息: %s", nick, user, " / ".join(events))

    # -- 等他说完，再决定怎么接
    def maybe_reload_live_cfg(self) -> None:
        """config.json 里这几段改完就生效，不用重启。

        整份配置仍然只在启动时读（改账号、密钥这种重启更稳），
        但节奏和表情包是纯行为参数，调一次重启一次太难受。
        """
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            return
        if mtime == self.cfg_mtime:
            return
        try:
            fresh = load_config()
        except Exception as exc:
            # 多半是刚存盘时 JSON 写坏了，先按住不换，下次扫描再试
            log.warning("config.json 读不出来，继续用旧参数：%s", exc)
            return
        self.cfg_mtime = mtime

        changed = []
        for sec in ("pacing", "burst", "stickers"):
            new = fresh.get(sec)
            if isinstance(new, dict) and new != self.cfg.get(sec):
                self.cfg[sec] = new
                changed.append(sec)
        if changed:
            p = self.cfg.get("pacing") or {}
            s = self.cfg.get("stickers") or {}
            log.info("参数已热更新（%s）：等 %s 秒 / 想 %s 秒 / 表情包概率 %s —— 不用重启",
                     "、".join(changed), p.get("settle_seconds"), p.get("think_seconds"),
                     s.get("use_probability"))

    def maybe_answer_pending(self) -> None:
        """每轮扫描后跑一次：哪条会话安静够久了，就回它。"""
        if self.paused or self._answering:
            return
        pcfg = self.cfg.get("pacing") or {}
        enabled = bool(pcfg.get("enabled", True))
        max_wait = float(pcfg.get("max_settle_seconds", 60) or 60)
        now = time.time()

        for user, item in list(self.pending.items()):
            texts = [t for t in (item.get("texts") or []) if t.strip()]
            parts = [p for p in (item.get("parts") or []) if str(p).strip()]
            if not parts:
                self.pending.pop(user, None)
                continue
            if not enabled:
                self.pending.pop(user, None)
                self._answer(user, texts, parts)
                continue

            # 等待时长按"这一轮一共几条"抽一次，之后固定，不每轮重抽。
            # 图片、表情包也算一条 —— 他先发张图再跟一句话，一样要等他说完。
            n_settle = max(1, len(parts))
            if item.get("wait") is None or item.get("n") != n_settle:
                item["wait"] = settle_delay(pcfg, n_settle)
                item["n"] = n_settle
            quiet = now - float(item.get("last_at") or now)
            total = now - float(item.get("first_at") or now)
            if quiet < float(item["wait"]) and total < max_wait:
                continue
            if quiet < 1.0:
                # 他刚发完（比如 max_wait 兜底到点了），再给一点缓冲，别打断
                continue

            self.pending.pop(user, None)
            self._answering = True
            try:
                self._answer(user, texts, parts)
            except Exception:
                log.error("回复 %s 出错:\n%s", user, traceback.format_exc())
            finally:
                self._answering = False

    def _should_stay_silent(self, user: str, texts: List[str]) -> bool:
        """这条要不要不接。

        只对"纯应答"放行，而且连着不回有上限。默认行为永远是回 ——
        沉默只是让对话偶尔有个自然的落脚点，不是让她玩消失。
        """
        pcfg = self.cfg.get("pacing") or {}
        if not pcfg.get("allow_silence", True):
            return False
        if not texts:                 # 只发了图 / 表情包，看不见也得应一声
            return False
        if len(texts) > 1:            # 连发几条的，一定回，别装没看见
            return False
        if not is_low_content(texts[0]):
            return False
        cap = int(pcfg.get("silence_max_streak", 1) or 0)
        if self._silence_streak.get(user, 0) >= cap:
            self._silence_streak[user] = 0
            return False
        if random.random() >= float(pcfg.get("silence_probability", 0.5)):
            return False
        self._silence_streak[user] = self._silence_streak.get(user, 0) + 1
        return True

    def _answer(self, user: str, texts: List[str], parts: Optional[List[str]] = None) -> None:
        """真正生成并发送。原来 handle_chat 的后半段搬到这里。

        ``texts`` 只有文字，用来判定等待和沉默；``parts`` 是完整的这一轮
        （文字原样 + 图片/表情包/文件那些方括号提示），给模型看的是后者。
        """
        now = time.time()
        nick = self.db.get_nickname(user) or user
        parts = [p for p in (parts or texts) if str(p).strip()]
        user_text = "\n".join(parts)
        multi = len(parts) > 1
        # 记忆和自学只吃纯文字：把「[他发来一个表情包]」当事实记下来会越记越乱
        plain = "\n".join(t for t in texts if t.strip()).strip()

        last = self.sent_times.get(user, 0)
        gap = self.behavior.get("reply_min_gap_same_chat", 4.0)
        if now - last < gap:
            # 和上一句挤在一起了，塞回缓冲，等下一轮再一起说，不丢消息
            log.info("%s 和上一条挨太近（%.1fs），再等一轮", nick, now - last)
            item = self.pending.setdefault(
                user, {"texts": [], "parts": [], "first_at": now, "last_at": now,
                       "wait": None, "n": 0}
            )
            item["texts"] = texts + list(item.get("texts") or [])
            item["parts"] = parts + list(item.get("parts") or [])
            item["last_at"] = now
            item["wait"] = None
            return
        self.send_log = [t for t in self.send_log if now - t < 3600]
        if len(self.send_log) >= self.behavior.get("global_max_per_hour", 120):
            log.warning("触发每小时发送上限，跳过 %s", nick)
            return

        if texts and self._should_stay_silent(user, texts):
            log.info("这条不接：%s「%s」——纯应答，这次就不回了", nick, user_text)
            return

        pcfg = self.cfg.get("pacing") or {}
        t0 = time.time()
        bubbles = self.reply_to(user, nick, user_text, multi=multi)
        sticker = self._pending_sticker

        # 生成本身就要几秒，算进"思考时间"里，不够再补一点
        if pcfg.get("enabled", True):
            need = think_delay(pcfg, user_text) - (time.time() - t0)
            if need > 0.2:
                time.sleep(need)

        scfg = self.sticker_cfg()
        # 概率闸只拦"文字配图"：她要是决定只发个表情，那就是这条回复本身，拦掉等于不回
        if sticker and bubbles and scfg.get("use_probability") is not None:
            if random.random() > float(scfg.get("use_probability", 0.75)):
                log.info("这次不配表情包（概率闸）")
                sticker = ""
        if not bubbles and not sticker:
            return

        sent_text = False
        if bubbles:
            sent_text = self.send_bubbles(user, bubbles, nick)
            if sent_text:
                self.sent_times[user] = time.time()
                self.send_log.append(time.time())
                self._silence_streak[user] = 0
                log.info("已回复 %s（%d 条）: %s", nick, len(bubbles), " ／ ".join(bubbles))
                if plain:
                    # 只发图/表情时没有文字，别拿空串去跑记忆提炼
                    self.remember_async(user, plain.replace("\n", " "), bubbles)
                    self.learn_async(plain, bubbles)

        # 文字发成功才补图；纯表情包的回复直接发图
        if sticker and (sent_text or not bubbles):
            if self.send_sticker(user, nick, sticker):
                self.sent_times[user] = time.time()
                self.send_log.append(time.time())
                if not bubbles:
                    log.info("已回复 %s（只发了个表情包）", nick)

    def reply_to(self, user: str, nick: str, text: str, multi: bool = False) -> List[str]:
        hist = self.state["history"].get(user, [])
        self._pending_sticker = ""      # 每轮重置，避免上一轮的表情指令漏到这一轮
        sys_prompt = self.build_system_prompt(text, user)
        if multi:
            # 他连着发了几条时，要当成一整段话回应。否则模型会逐条作答，
            # 你发三句它就分三段回，比抢答还像客服。
            sys_prompt += (
                "\n\n## 他在连着发\n"
                "他刚连着发了几条，是一个意思，不是三个问题。"
                "当成一整段听，挑真正值得接的那一点回，别一条一条答，"
                "也别提「你说了一二三」「你刚发了几条」这种话。"
            )
        if "[他发来" in text:
            sys_prompt += (
                "\n\n## 他发的是图，不是字\n"
                "- 抬头那几行方括号是程序加的，别说出口，也别写「你发了个表情包」这种话\n"
                "- 你看不到画面内容，也听不到语音。别夸图、别猜是什么图、别说「这张我见过」「收藏了」「听到了」\n"
                "- 也别解释自己为什么看不到（别提「我是文字模型」「我这边看不了」），"
                "就像图没加载出来一样自然带过：图没显出来 / 你直接说是啥\n"
                "- 别承诺帮他打开文件、听语音、看图，你做不到；直接问他里面是什么\n"
                "- 带情绪的（[他发来一个表情包：无语]）说明你认得这一张，顺着那个情绪说句人话，"
                "比如「又咋了」「这表情配你刚才那句挺合适」；别只发「……」或者「哦」\n"
                "- 没带情绪的，就按上下文接，或者直接问一句这啥意思\n"
                "- 只发了个表情包、一个字没说的时候，也要给个反应，别当没看见\n"
                "- 表情包过来往往是在逗你、吐槽、或者想让你笑一下，别当成正经问题答\n"
            )
        messages = [{"role": "system", "content": sys_prompt}]
        messages += hist[-self.behavior.get("history_turns", 8) * 2:]
        messages.append({"role": "user", "content": text})

        tools = self._desk_tools()
        if tools:
            try:
                got = deepseek_tool_chat(
                    self.cfg, messages, tools,
                    max_tokens=int(self.desk_cfg().get("max_tokens", 420)),
                )
            except Exception as exc:  # noqa: BLE001
                log.error("生成回复失败: %s", exc)
                return []
            if got["tool_calls"]:
                # 她要看电脑。这时候她还没说正文，先申请，批了再接着说。
                return self._request_desk(user, nick, messages, got["tool_calls"], 1, text)
            raw = got["content"]
        else:
            try:
                raw = deepseek_chat(self.cfg, messages)
            except Exception as exc:  # noqa: BLE001
                log.error("生成回复失败: %s", exc)
                return []

        if self.web_enabled() and SEARCH_RE.search(raw or ""):
            raw = self.maybe_web_answer(raw, messages, text)
            if not raw:
                return []
            # 查完资料的回答是一整段，不拆
            raw = raw.replace("|", "，").replace("｜", "，")

        raw, sticker = extract_sticker(raw or "")
        if sticker:
            self._pending_sticker = sticker

        bubbles = self.compose_bubbles(raw)
        if not bubbles:
            # 只发表情包不说话也算一条完整的回复
            if not sticker:
                return []
            hist.append({"role": "user", "content": text})
            hist.append({"role": "assistant", "content": f"[表情包:{sticker}]"})
            keep = self.behavior.get("history_turns", 8) * 2
            self.state["history"][user] = hist[-keep:]
            save_state(self.state)
            return []

        hist.append({"role": "user", "content": text})
        for b in bubbles:
            hist.append({"role": "assistant", "content": b})
        keep = self.behavior.get("history_turns", 8) * 2
        self.state["history"][user] = hist[-keep:]
        save_state(self.state)
        return bubbles

    def compose_bubbles(self, raw: str) -> List[str]:
        """决定这一轮拆不拆。拆分是概率性的 —— 每条都拆两截比不分还假。"""
        bcfg = self.cfg.get("burst") or {}
        max_n = max(1, int(bcfg.get("max_bubbles", 2)))
        max_chars = self.behavior.get("max_reply_chars", 50)
        max_sents = self.behavior.get("max_sentences", 2)

        text = (raw or "").strip()
        if not text:
            return []

        burst = (
            bcfg.get("enabled")
            and max_n > 1
            and random.random() < float(bcfg.get("probability", 0.35))
        )
        if not burst:
            # 不拆时必须抹掉分隔符，否则 | 会原样出现在微信里
            flat = clean_reply(text.replace("|", "，").replace("｜", "，"), max_chars, max_sents)
            return [flat] if flat else []

        out: List[str] = []
        for piece in split_bubbles(text, max_n):
            cleaned = clean_reply(piece, max_chars, max_sents)
            if cleaned:
                out.append(cleaned)
        return out[:max_n]

    def send_bubbles(self, user: str, bubbles: List[str], nick: str = "") -> bool:
        """逐条发出去，中间停顿模拟打字。第一条失败就放弃整轮，不硬发半句。"""
        if not bubbles:
            return False
        bcfg = self.cfg.get("burst") or {}
        gap = bcfg.get("gap_seconds") or [0.5, 1.4]
        try:
            lo, hi = float(gap[0]), float(gap[1])
        except (TypeError, IndexError, ValueError):
            lo, hi = 0.5, 1.4

        any_ok = False
        for i, b in enumerate(bubbles):
            if i > 0:
                time.sleep(random.uniform(lo, hi))
            if self.send(user, b, nick):
                any_ok = True
            else:
                log.error("连发第 %d/%d 条失败，放弃剩余 %d 条",
                          i + 1, len(bubbles), len(bubbles) - i - 1)
                break
        return any_ok

    def remember_async(self, user: str, user_text: str, replies: List[str]) -> None:
        """回复发出之后再提炼记忆，放在后台线程里，不拖慢回复。"""
        if self.mem is None or not (self.cfg.get("memory") or {}).get("enabled", True):
            return
        if len((user_text or "").strip()) < 4:
            return

        def work() -> None:
            try:
                from memory import extract_facts

                facts = extract_facts(
                    lambda msgs: deepseek_chat(self.cfg, msgs, max_tokens=500),
                    user_text, replies,
                )
                if not facts:
                    return
                with self._mem_lock:
                    added = self.mem.add(user, facts)
                if added:
                    log.info("记住 %d 条新事实（%s）：%s", added, user, " / ".join(facts))
            except Exception:  # noqa: BLE001
                log.error("记忆提取失败:\n%s", traceback.format_exc())

        threading.Thread(target=work, daemon=True, name="remember").start()

    def start_webui(self) -> None:
        wcfg = self.cfg.get("webui") or {}
        if not wcfg.get("enabled", True):
            log.info("Web 控制台未启用")
            return
        try:
            import webui
        except Exception as exc:  # noqa: BLE001
            log.warning("控制台模块不可用，跳过: %s", exc)
            return
        host = wcfg.get("host", "127.0.0.1")
        port = int(wcfg.get("port", 8765))
        if webui.start(self, host, port):
            log.info("控制台已就绪: http://%s:%s/", host, port)

    def resolve_target(self, user: str, nick: str) -> str:
        """优先用显示名发送：命中库里的「当前会话复用」快速路径，比传 wxid 快约 9 倍。

        但显示名必须唯一，否则拒绝使用（避免发给重名好友）。
        """
        if user in self._target_cache:
            return self._target_cache[user]
        target = user
        if nick:
            try:
                hits = [h for h in self.db.search_contact(nick) if h.get("username")]
                if len(hits) == 1 and hits[0]["username"] == user:
                    target = nick
                elif len(hits) > 1:
                    log.warning("显示名 %r 命中 %d 个联系人，回退用 wxid 发送", nick, len(hits))
            except Exception as exc:  # noqa: BLE001
                log.warning("解析联系人 %r 失败，回退 wxid: %s", nick, exc)
        self._target_cache[user] = target
        return target

    def send(self, user: str, text: str, nick: str = "") -> bool:
        """UIA 偶发 COM 瞬时错误（事件无法调用任何订户），重试三次。"""
        target = self.resolve_target(user, nick)
        last = None
        for attempt in range(3):
            try:
                if attempt == 0:
                    try:
                        self.gui.ensure_visible(keep_topmost=True)
                    except Exception:
                        pass
                else:
                    time.sleep(1.2 * attempt)
                resp = self.gui.send_msg(text, who=target, verify=False)
                log.info("send(%s) 返回: %s", target, resp)
                if isinstance(resp, dict) and resp.get("status") == "失败":
                    last = resp
                    if "结果未确认" in str(resp.get("message", "")):
                        break  # 已经可能发出去了，换目标重试会重复
                    continue
                return True
            except Exception as exc:  # noqa: BLE001
                last = exc
                log.warning("发送第 %d 次失败 %s: %s", attempt + 1, target, exc)
        # 显示名不通时，退回 wxid 再试一次
        if target != user:
            try:
                log.warning("改用 wxid 重试发送 %s", user)
                resp = self.gui.send_msg(text, who=user, verify=False)
                log.info("send(%s) 返回: %s", user, resp)
                if isinstance(resp, dict) and resp.get("status") == "成功":
                    return True
                last = resp
            except Exception as exc:  # noqa: BLE001
                last = exc
        log.error("发送最终失败 %s: %s", user, last)
        return False

    # -- 主动发消息 --------------------------------------------------
    def proactive_cfg(self) -> Dict[str, Any]:
        return self.cfg.get("proactive") or {}

    def proactive_enabled(self) -> bool:
        return bool(self.proactive_cfg().get("enabled"))

    def _proactive_target(self) -> Optional[str]:
        """config 指定优先；否则挑有历史记录、水位最新的那个会话。"""
        want = (self.proactive_cfg().get("target") or "").strip()
        if want:
            return want
        best, best_seq = None, -1
        for user in (self.state.get("history") or {}):
            if not is_replyable(user):
                continue
            seq = self.state.get("watermarks", {}).get(user, 0) or 0
            if seq > best_seq:
                best, best_seq = user, seq
        return best

    def _chat_stats(self, user: str) -> Tuple[float, int, int]:
        """(对方最后一条的时间, 当前最大 seq, 对方最后一条 seq)。

        get_messages 的返回顺序不保证，全部用 max 取，不依赖排列。
        """
        msgs = self.db.get_messages(user, limit=20) or []
        if not msgs:
            return 0.0, 0, 0
        last_t = max((m.get("create_time") or 0) for m in msgs)
        max_seq = max((m.get("sort_seq") or 0) for m in msgs)
        last_from_user = max(
            [(m.get("sort_seq") or 0) for m in msgs if m.get("sender_username") == user] or [0]
        )
        return float(last_t), int(max_seq), int(last_from_user)

    def proactive_decision(self, now: datetime, user: str) -> Tuple[Optional[str], str]:
        """此刻该不该主动说话。返回 (kind, 原因)，kind 为 None 表示不发。

        顺序就是优先级：早安 > 晚安 > 话题延续。原因串只用于日志和自测，
        方便事后回看"当时为什么没发"。
        """
        pcfg = self.proactive_cfg()
        now_min = now.hour * 60 + now.minute
        today = now.strftime("%Y-%m-%d")

        if in_quiet_hours(now_min, pcfg.get("quiet_hours") or ["23:00", "08:00"]):
            return None, "静默时段"

        rec = self.state.setdefault("proactive", {}).setdefault(user, {})
        if rec.get("date") != today:
            rec["date"] = today
            rec["count"] = 0
            rec["morning_date"] = ""
            rec["evening_date"] = ""
            rec.pop("followup_seq", None)

        last_t, max_seq, last_from_user = self._chat_stats(user)
        if not last_t:
            return None, "读不到聊天记录"

        # 上次主动发出去之后，对方回了没有？没回就不再追，真人不会自问自答。
        aw = int(rec.get("awaiting_seq") or 0)
        if aw:
            if last_from_user > aw:
                rec["awaiting_seq"] = 0
            elif pcfg.get("no_chase", True):
                idle_h = (now.timestamp() - float(rec.get("awaiting_at") or 0)) / 3600
                if idle_h < float(pcfg.get("chase_reset_hours", 12)):
                    return None, f"上次主动后对方未回（已 {idle_h:.1f}h）"
                rec["awaiting_seq"] = 0

        # 日上限只约束"话题延续"，不约束早晚问候。
        # 否则一聊得热，额度下午就烧完了，晚上的"晚安"永远发不出来。
        capped = int(rec.get("count") or 0) >= int(pcfg.get("max_per_day", 8))

        gap = float(pcfg.get("min_gap_minutes", 40)) * 60
        if rec.get("last_at"):
            since = now.timestamp() - float(rec["last_at"])
            if since < gap:
                return None, f"距上次主动仅 {since / 60:.0f} 分钟"

        mcfg = pcfg.get("morning") or {}
        if mcfg.get("enabled") and rec.get("morning_date") != today:
            if in_window(now_min, parse_hm(mcfg.get("start"), 8 * 60),
                         parse_hm(mcfg.get("end"), 9 * 60 + 30)):
                return "morning", "早间时段"

        ecfg = pcfg.get("evening") or {}
        if ecfg.get("enabled") and rec.get("evening_date") != today:
            if in_window(now_min, parse_hm(ecfg.get("start"), 21 * 60 + 30),
                         parse_hm(ecfg.get("end"), 22 * 60 + 50)):
                return "evening", "晚间时段"

        fcfg = pcfg.get("followup") or {}
        if capped:
            return None, "今日话题延续已达上限"
        if fcfg.get("enabled") and rec.get("followup_seq") != max_seq:
            idle_min = (now.timestamp() - last_t) / 60
            lo = float(fcfg.get("min_minutes", 45))
            hi = float(fcfg.get("max_minutes", 480))
            if lo <= idle_min <= hi:
                # 每个"静默段"抽一次额外延迟，避免分钟级精准（55、55、55…）。
                # 抽签结果按 max_seq 记账，同一次静默里不会每 60 秒重抽。
                jit = 0.0
                jcfg = float(fcfg.get("jitter_minutes", 0) or 0)
                if jcfg > 0:
                    if rec.get("followup_jitter_seq") != max_seq:
                        rec["followup_jitter_seq"] = max_seq
                        rec["followup_jitter"] = round(random.uniform(0, jcfg), 1)
                    jit = float(rec.get("followup_jitter") or 0)
                if idle_min >= lo + jit:
                    return "followup", f"话题挂起 {idle_min:.0f} 分钟"

        return None, "没有合适时机"

    def _pick_topic(self, hist: List[Dict[str, str]]) -> str:
        """从最近几轮里找出一个"真的能接下去"的话题，找不到返回空串。

        单独做一次判定而不是让模型边想边写：生成时它倾向于凑一个像样的话题，
        哪怕上文根本没提过。判定任务只看原话、不做创作，编造率低得多。
        """
        recent = "\n".join(
            f"{'对方' if m.get('role') == 'user' else '我'}: {m.get('content')}"
            for m in hist[-6:]
        )
        if not recent:
            return ""
        messages = [
            {"role": "system", "content": "你在判断一段微信聊天里有没有还能接着聊的具体话题。严格只看原文。"},
            {"role": "user", "content": (
                f"聊天记录：\n{recent}\n\n"
                "里面有没有具体的事值得再问一句？比如对方提过的计划、没答完的问题、"
                "正在做的东西。\n"
                "有 → 只输出一行：YES: 十个字以内概括那个话题\n"
                "没有 → 只输出：NO\n"
                "不要脑补记录里没出现的人、物、事。"
            )},
        ]
        try:
            out = deepseek_chat(self.cfg, messages, max_tokens=600)
        except Exception as exc:  # noqa: BLE001
            log.warning("话题判定失败，按无话题处理: %s", exc)
            return ""
        m = re.search(r"YES\s*[:：]\s*(.+)", out or "", re.I)
        if not m:
            return ""
        topic = m.group(1).strip().strip("。，、；:：\"'「」“” ")
        return topic[:20]

    def _gen_proactive(self, user: str, nick: str, kind: str,
                       now: Optional[datetime] = None) -> Optional[str]:
        now = now or datetime.now()
        hist = (self.state.get("history") or {}).get(user, []) or []

        last_user_msg = ""
        for m in reversed(hist):
            if m.get("role") == "user":
                last_user_msg = m.get("content") or ""
                break

        if kind == "morning":
            ask, query = "发一句早安。可以顺带一件今天的小事，但别像天气预报。", "早安"
        elif kind == "evening":
            ask, query = "说一句晚安。自然点，别煽情，别提明天几点起。", "晚安"
        else:
            topic = self._pick_topic(hist)
            if topic:
                log.info("话题延续命中：%s", topic)
                ask = f"接着「{topic}」这个话题再问一句，像突然想起来那样。"
                query = topic
            else:
                log.info("没有可接的话题，改成日常问候")
                ask = "随口问一句对方这会儿在忙什么。"
                query = last_user_msg or "继续聊"

        messages = [{"role": "system", "content": self.build_system_prompt(query, user)}]
        messages += hist[-self.behavior.get("history_turns", 8) * 2:]
        messages.append({
            "role": "user",
            "content": (
                f"（现在是 {now.strftime('%H:%M')}，请主动给 {nick} 发一条微信：）\n{ask}\n\n"
                "只输出要发的那句话本身，一句话，不超过 20 字。"
                "不要「在吗」「睡了吗」这种空话，直接说内容。"
                "也不要说「我主动」「这条消息」「没打扰你吧」这类关于聊天本身的元话。"
                "绝对不要编造上文没提过的事情（没提过的方案、文件、地点、人名都不能出现）。"
                "不要输出 SEARCH。"
            ),
        })

        try:
            raw = deepseek_chat(self.cfg, messages, max_tokens=400)
        except Exception as exc:  # noqa: BLE001
            log.error("主动消息生成失败: %s", exc)
            return None

        # 主动消息只发一条，模型给的 | 直接抹平
        flat = (raw or "").replace("|", "，").replace("｜", "，")
        text = clean_reply(flat, min(30, self.behavior.get("max_reply_chars", 32)), 2)
        text = SEARCH_RE.sub("", text or "").strip("，。、；： ")
        return text or None

    def maybe_proactive(self, force_kind: Optional[str] = None) -> Optional[str]:
        """每轮扫描后调用一次。内部 60 秒节流，避免每 2 秒查一次库。

        force_kind 给控制台用：绕过时间判定和节流，直接生成并发出，返回发出的文本。
        """
        if self.paused and not force_kind:
            return None
        if not self.proactive_enabled() and not force_kind:
            return None
        now_ts = time.time()
        if not force_kind and now_ts - self._last_proactive_check < 60:
            return None
        self._last_proactive_check = now_ts

        user = self._proactive_target()
        if not user:
            log.warning("没有可主动发起的会话（state.history 为空）")
            return None
        try:
            nick = self.db.get_nickname(user) or user
        except Exception:  # noqa: BLE001
            nick = user

        if force_kind:
            kind, why = force_kind, "手动触发"
        else:
            kind, why = self.proactive_decision(datetime.now(), user)
            if not kind:
                # 只在原因变化时记一行，否则每 60 秒刷一次日志
                if why != self._last_proactive_why:
                    log.info("暂不主动发：%s", why)
                    self._last_proactive_why = why
                return None
            self._last_proactive_why = ""

        text = self._gen_proactive(user, nick, kind)
        if not text:
            log.warning("主动消息(%s)内容为空，跳过", kind)
            return None

        _, max_seq, _ = self._chat_stats(user)
        if not self.send(user, text, nick):
            log.error("主动消息(%s)发送失败", kind)
            return None

        now = datetime.now()
        rec = self.state.setdefault("proactive", {}).setdefault(user, {})
        rec["count"] = int(rec.get("count") or 0) + 1
        rec["last_at"] = now.timestamp()
        rec["awaiting_seq"] = max_seq
        rec["awaiting_at"] = now.timestamp()
        if kind == "morning":
            rec["morning_date"] = now.strftime("%Y-%m-%d")
        elif kind == "evening":
            rec["evening_date"] = now.strftime("%Y-%m-%d")
        else:
            rec["followup_seq"] = max_seq

        # 记进历史，否则下一轮对话模型不知道自己刚说过这句
        hist = self.state["history"].setdefault(user, [])
        hist.append({"role": "assistant", "content": text})
        keep = self.behavior.get("history_turns", 8) * 2
        self.state["history"][user] = hist[-keep:]
        save_state(self.state)
        log.info("主动消息(%s/%s)已发 %s: %s", kind, why, nick, text.replace("\n", " "))
        return text

    def run(self) -> None:
        self.bind()
        self.load_persona(force=True)
        self.init_knowledge()
        self.init_memory()
        self.init_curiosity()
        self.init_stickers()
        self.init_inbox()
        self.init_desk()
        self.start_webui()
        log.info("开始监听（Ctrl+C 退出）……")
        while True:
            try:
                self.poll_once()
                self.maybe_reload_live_cfg()
                self.maybe_answer_pending()
                self.maybe_resume_desk()
                self.maybe_proactive()
                self.maybe_learn()
                self.maybe_collect_stickers()
                if self.kb is not None:
                    # 自学写了新笔记时，这里会顺手把知识库索引更新掉（内部按 mtime 判重，很便宜）
                    self.kb.maybe_reload()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.error("本轮扫描异常:\n%s", traceback.format_exc())
            time.sleep(self.behavior.get("poll_interval", 2.0))


def acquire_single_instance() -> bool:
    """防止双开。两个实例会重复回复同一条消息，还会互踩 state.json 的水位。"""
    import psutil

    lock = BASE / "bot.pid"
    if lock.exists():
        try:
            pid = int(lock.read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            pid = 0
        if pid and pid != os.getpid():
            try:
                p = psutil.Process(pid)
                if p.is_running() and "bridge.py" in " ".join(p.cmdline() or []):
                    log.error(
                        "已经有一个实例在运行（pid=%s）。同时开两个会把每条消息回两遍，"
                        "本次启动取消。要重启请先关掉原来的窗口。",
                        pid,
                    )
                    return False
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    try:
        lock.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        log.warning("无法写入 %s，双开保护失效（不影响使用）", lock.name)
    return True


def main() -> int:
    setup_logging()
    if not acquire_single_instance():
        return 2
    try:
        cfg = load_config()
        # 写入节奏档：natural 每次写操作要等 2.5~6 秒，聊天场景太慢
        os.environ.setdefault("WECHATAUTO_RHYTHM", cfg.get("rhythm", "fast"))
        Bridge(cfg).run()
    except KeyboardInterrupt:
        log.info("已手动停止")
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("启动失败: %s", exc)
        log.error(traceback.format_exc())
        return 1
    finally:
        try:
            (BASE / "bot.pid").unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
