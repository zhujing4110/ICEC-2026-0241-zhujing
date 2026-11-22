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
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

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
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
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

def deepseek_chat(cfg: Dict[str, Any], messages: List[Dict[str, str]]) -> str:
    d = cfg["deepseek"]
    body = {
        "model": d["model"],
        "messages": messages,
        "temperature": d.get("temperature", 1.0),
        "max_tokens": d.get("max_tokens", 800),
        "stream": False,
    }
    # 思考链越长，回答越像在"写报告"。聊天场景压到 low，句子会短很多。
    effort = d.get("reasoning_effort")
    if effort:
        body["reasoning_effort"] = effort
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        d["base_url"].rstrip("/") + "/chat/completions",
        data=data,
        headers={
            "Authorization": "Bearer " + d["api_key"],
            "Content-Type": "application/json",
        },
    )
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=d.get("timeout", 60)) as r:
                payload = json.load(r)
            return (payload["choices"][0]["message"]["content"] or "").strip()
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("DeepSeek 第 %d 次失败: %s", attempt + 1, exc)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"DeepSeek 调用失败: {last}")

# ---------------------------------------------------------------- 清理回复文本

MD_PAT = re.compile(r"[*_`#>]+")
# 模型想联网时约定只回一行 "SEARCH: 关键词"，这里负责识别
SEARCH_RE = re.compile(r"SEARCH\s*[:：]\s*(.+)", re.I)
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
        self.kb = None
        self.db = None
        self.gui = None
        self.account = None
        self.hwnd = None
        self.self_username = None

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

    def build_system_prompt(self, query: str) -> str:
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
        if self.web_enabled():
            parts.append(
                "## 联网\n"
                "你看不到实时信息（天气、新闻、比分、票价、最近发生的事）。"
                "遇到这类问题先查一下：**整条回复只输出一行**\n"
                "SEARCH: 搜索关键词\n"
                "其余情况正常聊天，绝对不要出现 SEARCH 这三个字母。"
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
        log.info("已绑定账号: %s (%s)  hwnd=%s", nick, self.self_username, self.hwnd)

        self.gui = WeChatGUI(hwnd=self.hwnd)
        if not self.gui.is_alive():
            raise RuntimeError("微信窗口绑定失败")

    # -- 每轮扫描
    def poll_once(self) -> None:
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
            and m.get("type") == "文本"
            and (m.get("content") or "").strip()
        ]
        if not incoming:
            return

        self.state["watermarks"][user] = max(m.get("sort_seq", 0) for m in msgs)
        save_state(self.state)

        now = time.time()
        last = self.sent_times.get(user, 0)
        gap = self.behavior.get("reply_min_gap_same_chat", 4.0)
        if now - last < gap:
            log.info("跳过 %s：距上次回复 %.1fs < %.1fs", user, now - last, gap)
            return
        self.send_log = [t for t in self.send_log if now - t < 3600]
        if len(self.send_log) >= self.behavior.get("global_max_per_hour", 120):
            log.warning("触发每小时发送上限，跳过 %s", user)
            return

        user_text = "\n".join((m.get("content") or "").strip() for m in incoming)
        nick = self.db.get_nickname(user) or user
        log.info("收到 %s(%s): %s", nick, user, user_text.replace("\n", " / "))

        reply = self.reply_to(user, nick, user_text)
        if not reply:
            return

        ok = self.send(user, reply, nick)
        if ok:
            self.sent_times[user] = time.time()
            self.send_log.append(time.time())
            log.info("已回复 %s: %s", nick, reply.replace("\n", " / "))

    def reply_to(self, user: str, nick: str, text: str) -> Optional[str]:
        hist = self.state["history"].get(user, [])
        messages = [{"role": "system", "content": self.build_system_prompt(text)}]
        messages += hist[-self.behavior.get("history_turns", 8) * 2:]
        messages.append({"role": "user", "content": text})

        try:
            raw = deepseek_chat(self.cfg, messages)
        except Exception as exc:  # noqa: BLE001
            log.error("生成回复失败: %s", exc)
            return None

        if self.web_enabled() and SEARCH_RE.search(raw or ""):
            raw = self.maybe_web_answer(raw, messages, text)
            if not raw:
                return None

        reply = clean_reply(
            raw,
            self.behavior.get("max_reply_chars", 50),
            self.behavior.get("max_sentences", 2),
        )
        if not reply:
            return None

        hist.append({"role": "user", "content": text})
        hist.append({"role": "assistant", "content": reply})
        keep = self.behavior.get("history_turns", 8) * 2
        self.state["history"][user] = hist[-keep:]
        save_state(self.state)
        return reply

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

    def run(self) -> None:
        self.bind()
        self.load_persona(force=True)
        self.init_knowledge()
        log.info("开始监听（Ctrl+C 退出）……")
        while True:
            try:
                self.poll_once()
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
