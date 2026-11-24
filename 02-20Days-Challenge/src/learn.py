"""自主好奇：把她没把握的东西排成队列，空闲时自己上网查、写成笔记。

三件事：
1. 发现 —— 从聊天里挑出"她不确定、但值得查"的具体名词
2. 沉淀 —— 空闲时搜一遍、读正文、写进 knowledge/learned/，自动进知识库
3. 约束 —— 每日上限、静默时段、你不说话时才学、同一话题不重复

刻意不做的事：不在学到东西后主动炫耀。她只在相关对话里"正好知道"。
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

LLM = Callable[..., str]

PUNCT_RE = re.compile(r"[\s，。！？、；：\"'“”‘’（）()《》…—\-·|/\\]+")
ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\s]+')
LINE_RE = re.compile(r"^\s*(?:[-*•]|\d+[.、)])\s*")
TOPIC_RE = re.compile(r"TOPIC[:：]\s*(.+)$", re.I)


def _norm(text: str) -> str:
    return PUNCT_RE.sub("", (text or "").lower())


def slugify(topic: str, limit: int = 28) -> str:
    """给笔记起文件名。中文保留，非法字符和空格换成下划线。"""
    s = ILLEGAL_RE.sub("_", (topic or "").strip()).strip("._")
    return (s or "note")[:limit]


# ---------------------------------------------------------------- 队列


class Curiosity:
    """待学队列 + 学习计数，一个人一个 json，操作时加锁重读，避免线程互踩。"""

    def __init__(self, path: Path, learned_dir: Path, max_pending: int = 40):
        self.path = Path(path)
        self.learned_dir = Path(learned_dir)
        self.max_pending = max(1, int(max_pending))
        self._lock = threading.Lock()

    # -- 文件
    def _read(self) -> Dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("items", [])
                data.setdefault("meta", {})
                return data
        except (OSError, ValueError):
            pass
        return {"items": [], "meta": {}}

    def _write(self, data: Dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # -- 队列
    def add(self, items: List[Dict[str, str]], source: str = "chat") -> List[str]:
        """去重后入队。返回真正新增的话题。"""
        with self._lock:
            data = self._read()
            seen = {it.get("id") for it in data["items"]}
            added: List[str] = []
            for it in items or []:
                topic = (it.get("topic") or "").strip()
                if not (2 <= len(topic) <= 24):
                    continue
                tid = _norm(topic)
                if not tid:
                    continue
                if tid in seen:
                    # 之前放弃过的，再提一次就当重新排队（方便手动重试）
                    for old in data["items"]:
                        if old.get("id") == tid and old.get("status") == "skipped":
                            old["status"] = "pending"
                            old["source"] = source
                            added.append(old.get("topic", topic))
                            break
                    continue
                if (self.learned_dir / (slugify(topic) + ".md")).exists():
                    continue
                data["items"].append({
                    "id": tid,
                    "topic": topic,
                    "why": (it.get("why") or "").strip()[:60],
                    "source": source,
                    "created_at": time.time(),
                    "status": "pending",
                    "tries": 0,
                    "note": "",
                })
                seen.add(tid)
                added.append(topic)

            pending = [x for x in data["items"] if x.get("status") == "pending"]
            if len(pending) > self.max_pending:
                drop = sorted(pending, key=lambda x: x.get("created_at") or 0)[:len(pending) - self.max_pending]
                drop_ids = {x["id"] for x in drop}
                data["items"] = [x for x in data["items"] if x["id"] not in drop_ids]
            done = [x for x in data["items"] if x.get("status") == "done"]
            if len(done) > 30:
                keep = {x["id"] for x in sorted(done, key=lambda x: x.get("learned_at") or 0)[-30:]}
                data["items"] = [x for x in data["items"] if x.get("status") != "done" or x["id"] in keep]

            self._write(data)
            return added

    def next_pending(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            data = self._read()
            pending = [x for x in data["items"] if x.get("status") == "pending"]
            if not pending:
                return None
            # 聊天里带出来的优先，其次是试过次数少的、放得久的
            pending.sort(key=lambda x: (0 if x.get("source") == "chat" else 1,
                                        x.get("tries") or 0,
                                        x.get("created_at") or 0))
            return dict(pending[0])

    def mark(self, tid: str, status: str, note: str = "", bump_tries: bool = True) -> None:
        with self._lock:
            data = self._read()
            for it in data["items"]:
                if it.get("id") == tid:
                    it["status"] = status
                    if note:
                        it["note"] = note[:80]
                    if bump_tries:
                        it["tries"] = int(it.get("tries") or 0) + 1
                    if status == "done":
                        it["learned_at"] = time.time()
                    break
            self._write(data)

    def revive(self, limit: int = 2, max_tries: int = 2) -> int:
        """把之前没查成的重新排队。搜索引擎抽风常常是暂时的，给它第二次机会。"""
        with self._lock:
            data = self._read()
            cands = [x for x in data["items"]
                     if x.get("status") == "skipped" and int(x.get("tries") or 0) < max_tries]
            cands.sort(key=lambda x: x.get("created_at") or 0)
            for x in cands[:limit]:
                x["status"] = "pending"
            self._write(data)
            return min(len(cands), limit)

    def remove(self, tid: str) -> bool:
        with self._lock:
            data = self._read()
            before = len(data["items"])
            data["items"] = [x for x in data["items"] if x.get("id") != tid]
            self._write(data)
            return len(data["items"]) != before

    def items(self) -> List[Dict[str, Any]]:
        return self._read().get("items", [])

    # -- 计数（存在同一个文件里，不碰 state.json，避免和主线程抢写）
    def today_count(self) -> int:
        meta = self._read().get("meta", {})
        if meta.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return 0
        return int(meta.get("count") or 0)

    def last_learn_at(self) -> float:
        return float(self._read().get("meta", {}).get("last_at") or 0)

    def bump(self) -> int:
        with self._lock:
            data = self._read()
            meta = data.setdefault("meta", {})
            today = datetime.now().strftime("%Y-%m-%d")
            if meta.get("date") != today:
                meta["date"] = today
                meta["count"] = 0
            meta["count"] = int(meta.get("count") or 0) + 1
            meta["last_at"] = time.time()
            self._write(data)
            return meta["count"]

    def stats(self) -> Dict[str, int]:
        items = self.items()
        return {
            "pending": sum(1 for x in items if x.get("status") == "pending"),
            "done": sum(1 for x in items if x.get("status") == "done"),
            "skipped": sum(1 for x in items if x.get("status") == "skipped"),
            "today": self.today_count(),
        }


# ---------------------------------------------------------------- 笔记


def learned_notes(learned_dir: Path) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    if not learned_dir.is_dir():
        return out
    for p in sorted(learned_dir.glob("*.md")):
        try:
            head = p.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        topic = head[0].lstrip("# ").strip() if head else p.stem
        at = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
        out.append({"topic": topic, "file": p.name, "at": at})
    out.sort(key=lambda x: x["at"], reverse=True)
    return out


def delete_note(learned_dir: Path, name: str) -> bool:
    p = (Path(learned_dir) / name).resolve()
    if p.parent != Path(learned_dir).resolve() or p.suffix.lower() != ".md":
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- 让她自己去查


def extract_curiosity(llm: LLM, user_text: str, reply: str) -> List[Dict[str, str]]:
    """从一轮对话里挑出她其实不确定的具体名词。挑不出就返回空。"""
    msgs = [
        {"role": "system",
         "content": "你在检查自己刚聊过的一轮话：里面有没有出现你其实没把握、但值得查一下的具体东西。"
                    "只看具体名词——工具、产品、地方、事件、专业说法。情绪、天气、寒暄、你的感受都不算。"},
        {"role": "user",
         "content": f"对方说：{(user_text or '')[:200]}\n你回：{(reply or '')[:200]}\n\n"
                    "最多挑 2 个。每行一条，格式：\nTOPIC: 名称 | 一句话说明你为什么要查\n"
                    "一个都没有就只输出：NONE"},
    ]
    try:
        out = llm(msgs, 600)
    except Exception:  # noqa: BLE001
        return []
    items: List[Dict[str, str]] = []
    for line in (out or "").splitlines():
        line = LINE_RE.sub("", line).strip()
        if not line or line.upper().startswith("NONE"):
            continue
        m = TOPIC_RE.search(line)
        if not m:
            continue
        body = m.group(1)
        topic, _, why = body.partition("|")
        items.append({"topic": topic.strip(" 　\"'“”"), "why": why.strip()})
        if len(items) >= 2:
            break
    return items


def invent_topic(llm: LLM, interests: str) -> Optional[Dict[str, str]]:
    """队列空了，让她按自己的兴趣提一个真想搞明白的具体问题。"""
    msgs = [
        {"role": "system", "content": "你是个有好奇心的人，会自己上网看东西。说话要具体，不列清单。"},
        {"role": "user",
         "content": f"你的兴趣大概在：{interests}\n\n"
                    "想一个你最近真想搞明白的具体问题（不要泛泛的领域，要能搜到答案的），"
                    "12 字以内，只输出一行：TOPIC: 问题"},
    ]
    try:
        out = llm(msgs, 600)
    except Exception:  # noqa: BLE001
        return None
    for line in (out or "").splitlines():
        m = TOPIC_RE.search(LINE_RE.sub("", line.strip()))
        if m:
            topic = m.group(1).strip(" 　\"'“”")
            if 2 <= len(topic) <= 24:
                return {"topic": topic, "why": "自己好奇"}
    return None


def _norm_cn(text: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffa-zA-Z0-9]", "", (text or "").lower())


def _longest_common(a: str, b: str) -> int:
    """最长公共子串长度。短字符串够用了。"""
    if not a or not b:
        return 0
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _bigrams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)}


# 疑问词和语气词不参与相关性判断：问题句里它们只会拉低命中率
ASK_NOISE_RE = re.compile(
    r"(请问|想问|一下|到底|究竟|是不是|能否|可以|能不能|会不会|有没有|"
    r"怎么样|怎么|如何|为什么|啥|什么|哪|多少|吗|呢|吧|啊|呀|的|了|"
    r"是否|应该|需要|要)"
)


def _relevant(topic: str, results: List[Dict[str, str]]) -> bool:
    """搜索结果和问题对不对得上。

    Bing 偶尔会把中文查询切坏（"微信自动化" 只搜 "微"），返回一堆不相关的页面。
    这种结果宁可跳过，也不写成笔记 —— 笔记进知识库后会被当成她"知道"的事。

    判定用二元组覆盖率：比"连续公共子串"宽容，提问句（"…能商用吗"）也能正确放行。
    """
    key = _norm_cn(topic)
    if not key:
        return False
    core = _norm_cn(ASK_NOISE_RE.sub("", topic)) or key
    grams = _bigrams(core)
    for r in results[:3]:
        text = _norm_cn(f"{r.get('title', '')}{r.get('snippet', '')}")
        if not text:
            continue
        if not grams:
            if core in text:
                return True
            continue
        hit = sum(1 for g in grams if g in text) / len(grams)
        if hit >= 0.45 or _longest_common(core, text) >= max(3, int(len(core) * 0.6)):
            return True
    return False


def _search_queries(topic: str, llm: LLM) -> List[str]:
    """把问题压成短关键词。Bing 对长中文/中英混排查询会切错词，短词命中率高得多。"""
    out: List[str] = []
    try:
        raw = llm([
            {"role": "system", "content": "你把一个问题改写成用于搜索引擎的关键词。只输出关键词，不要标点、不要解释。"},
            {"role": "user", "content": f"问题：{topic}\n\n"
                                        "给 2 组关键词，每组不超过 10 个字、不含问句语气，一行一组。"
                                        "一组带具体名词，一组更短更宽泛。"},
        ], 500)
        for line in (raw or "").splitlines():
            q = re.sub(r"^[\s\-*•\d.、)）]+", "", line).strip(" 　\"'“”")
            if 2 <= len(q) <= 20 and q not in out:
                out.append(q)
    except Exception:  # noqa: BLE001
        pass
    if topic not in out:
        out.append(topic)
    return out[:3]


def learn_one(topic: str, llm: LLM, learned_dir: Path, note_max_chars: int = 600) -> Dict[str, Any]:
    """搜 → 读 → 写笔记。返回 {ok, file, summary} 或 {ok: False, reason}。"""
    import websearch

    results: List[Dict[str, str]] = []
    extra = ""
    used = topic
    for q in _search_queries(topic, llm):
        results, extra = websearch.search_and_read(q, top=4, read_top=1)
        if results and _relevant(q, results):
            used = q
            break
        results, extra = [], ""
    if not results:
        return {"ok": False, "reason": "搜到的结果和问题对不上"}

    material = websearch.format_for_prompt(results, extra)
    msgs = [
        {"role": "system",
         "content": "你在给自己做一份极短的读书笔记。只写资料里有的内容，不编、不引申、不评价。"},
        {"role": "user",
         "content": f"要搞明白的东西：{topic}\n\n查到的资料：\n{material}\n\n"
                    "写 2 到 4 句，不超过 150 字：它是什么、关键点是什么。\n"
                    "资料里没解释清楚就只输出：没查到\n"
                    "不要写“根据资料”“综上”“可以看出”这类话，直接说内容。"},
    ]
    try:
        summary = (llm(msgs, 800) or "").strip()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"模型调用失败：{exc}"}

    summary = re.sub(r"^(好的|笔记[:：])\s*", "", summary).strip()
    if len(summary) < 12 or summary.startswith("没查到"):
        return {"ok": False, "reason": "资料里没有可用解释", "raw": summary[:60]}

    summary = summary[:note_max_chars]
    learned_dir = Path(learned_dir)
    learned_dir.mkdir(parents=True, exist_ok=True)
    path = learned_dir / (slugify(topic) + ".md")
    host = urllib.parse.urlparse(results[0]["url"]).netloc or "网上"
    searched = "" if used == topic else f"，查的是「{used}」"
    body = (
        f"# {topic}\n\n"
        f"> 我自己上网查的（{datetime.now().strftime('%Y-%m-%d')}），来源 {host}{searched}。"
        f"记得不一定准，说的时候留点余地。\n\n"
        f"{summary}\n\n"
        "## 来源\n" + "\n".join(f"- {r['url']}" for r in results[:3]) + "\n"
    )
    tmp = path.with_suffix(".tmp")
    tmp.write_text(body, encoding="utf-8")
    tmp.replace(path)
    return {"ok": True, "file": path.name, "summary": summary,
            "urls": [r["url"] for r in results[:3]]}
