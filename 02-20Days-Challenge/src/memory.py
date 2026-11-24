#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""长期记忆：把聊天里关于对方的稳定事实记下来，跨天、跨会话都能用。

和知识库的区别：
- knowledge/ 是**你写给它**的资料（她是谁、怎么回答常见问题）
- memory/    是**它自己从聊天里学到的**（对方在赶网页设计大作业）

存储：memory/{安全化的 wxid}.json，一人一个文件，可以直接打开看或手改。
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

EXTRACT_PROMPT = """从下面这轮微信对话里，找出关于「对方」值得长期记住的事实。

只记稳定的、以后真用得上的：
- 对方在做什么事（学业、工作、项目）
- 对方的困扰、目标、计划
- 对方的喜好、习惯、称呼

不要记：寒暄、情绪波动、一次性的时间安排、你自己说过的话。

每条一行，不超过 15 个字，最多 3 条。
没有值得记的就只输出 NONE。"""


def _safe_name(user: str) -> str:
    """wxid 里可能带 @ 或空格，落到文件名上要洗干净。"""
    return re.sub(r"[^0-9A-Za-z_-]+", "_", user or "unknown")[:60]


def _bigrams(text: str) -> set:
    """中文二元组，做粗粒度相关性排序。不引 jieba，够用且快。"""
    s = re.sub(r"\s+", "", text or "")
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


class Memory:
    def __init__(self, base: Path, max_items: int = 40):
        self.dir = Path(base) / "memory"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_items = max_items
        self._lock = threading.Lock()

    def _path(self, user: str) -> Path:
        return self.dir / f"{_safe_name(user)}.json"

    def load(self, user: str) -> List[Dict[str, Any]]:
        p = self._path(user)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []
        items = data.get("items") if isinstance(data, dict) else data
        return [it for it in (items or []) if isinstance(it, dict) and it.get("text")]

    def save(self, user: str, items: List[Dict[str, Any]]) -> None:
        items = items[-self.max_items:]
        p = self._path(user)
        tmp = p.with_suffix(".tmp")
        payload = {"user": user, "updated": time.time(), "items": items}
        with self._lock:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(p)

    def add(self, user: str, texts: List[str]) -> int:
        """按文本去重后追加，超出上限丢最旧的。返回真正新增的条数。"""
        clean = []
        for t in texts:
            t = (t or "").strip()
            if t:
                clean.append(t[:40])
        if not clean:
            return 0

        items = self.load(user)
        have = {it["text"] for it in items}
        added = 0
        for t in clean:
            if t in have:
                continue
            items.append({"text": t, "ts": time.time()})
            have.add(t)
            added += 1
        if added:
            self.save(user, items)
        return added

    def _rank(self, items: List[Dict[str, Any]], query: str, limit: int):
        if len(items) <= limit:
            return items
        q = _bigrams(query)
        scored = []
        for it in items:
            overlap = len(q & _bigrams(it["text"])) if q else 0
            scored.append((overlap, it.get("ts") or 0, it))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return [x[2] for x in scored[:limit]]

    def context_for(self, user: str, query: str = "", limit: int = 6) -> str:
        items = self.load(user)
        if not items:
            return ""
        picked = self._rank(items, query, limit)
        return "\n".join("- " + it["text"] for it in picked)

    def stats(self, user: str) -> Dict[str, Any]:
        items = self.load(user)
        return {
            "count": len(items),
            "updated": max((it.get("ts") or 0) for it in items) if items else 0,
            "items": items,
        }

    def users(self) -> List[str]:
        out = []
        for p in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                out.append(data.get("user") or p.stem)
            except Exception:
                continue
        return out


def extract_facts(llm: Callable[[List[Dict[str, str]]], str],
                  user_text: str,
                  reply_texts: Optional[List[str]] = None) -> List[str]:
    """让模型从这一轮对话里挑出值得长期记的事实。失败一律返回空，不影响主流程。"""
    convo = "对方：" + (user_text or "").strip()
    if reply_texts:
        convo += "\n你：" + " ".join(reply_texts)
    if len(convo) < 6:
        return []

    try:
        out = llm([
            {"role": "system", "content": EXTRACT_PROMPT},
            {"role": "user", "content": "对话：\n" + convo},
        ])
    except Exception:  # noqa: BLE001
        return []

    facts: List[str] = []
    for line in (out or "").splitlines():
        line = line.strip().strip("-·•*0123456789.、)） ")
        if not line:
            continue
        if line.upper() in {"NONE", "无", "没有"}:
            continue
        if len(line) < 3 or len(line) > 30:
            continue
        if line not in facts:
            facts.append(line)
    return facts[:3]
