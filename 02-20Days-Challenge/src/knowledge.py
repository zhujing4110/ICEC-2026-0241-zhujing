#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""极简本地知识库：中文分词 + BM25 检索，零外部服务、零向量库。

用法：
    kb = KnowledgeBase(Path("knowledge"))
    block = kb.context_for("你们能做什么设计", max_chars=900)
    # block 为空字符串表示没命中，别硬塞进 prompt

设计取舍：
- 微信聊天的问题普遍很短，向量检索要额外依赖和费用，BM25 在这个量级（几十个文件）
  足够用，而且改完文件即生效，不需要重建向量。
- 宁愿不注入，也不要注入不相关的段落——闲聊被塞进资料会让人设崩掉，
  所以有 min_score 门槛。
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

try:
    import jieba
except Exception as exc:  # pragma: no cover
    raise RuntimeError("缺少 jieba，请先 pip install jieba") from exc

PATHS_EXT = {".md", ".txt", ".markdown"}
STOPWORDS = set(
    "的 了 是 在 我 你 他 她 它 有 和 就 不 人 都 一 一个 上 也 很 到 说 要 去 会 着 没有 "
    "看 好 自己 这 那 什么 怎么 为什么 可以 能 吗 呢 吧 啊 呀 哦 嗯 个 们 与 及 或 被 把 "
    "the a an is are to of and in on for it this that you i".split()
)
TOKEN_RE = re.compile(r"[0-9a-zA-Z_]+|[\u4e00-\u9fff]+")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text: str) -> List[str]:
    out: List[str] = []
    for piece in TOKEN_RE.findall(text.lower()):
        if piece.isascii():
            out.append(piece)
            continue
        for w in jieba.lcut_for_search(piece):
            w = w.strip()
            if len(w) < 2 and not w.isascii():
                continue
            if w in STOPWORDS:
                continue
            if not w:
                continue
            out.append(w)
    return out


@dataclass
class Chunk:
    source: str          # 文件名
    heading: str         # 所属小标题
    text: str            # 正文
    tokens: List[str] = field(default_factory=list)
    tf: Dict[str, int] = field(default_factory=dict)
    bigrams: set = field(default_factory=set)

    def render(self) -> str:
        head = f"【{self.source}"
        if self.heading:
            head += f" · {self.heading}"
        head += "】"
        return f"{head}\n{self.text}"


def _cjk_bigrams(text: str) -> set:
    """中文按字二元组切，用来兜住分词切不出来的口语说法。"""
    out = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        for i in range(len(run) - 1):
            out.add(run[i:i + 2])
    return out


class KnowledgeBase:
    def __init__(self, root: Path, chunk_size: int = 320, overlap: int = 40):
        self.root = Path(root)
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.chunks: List[Chunk] = []
        self._df: Dict[str, int] = {}
        self._avg_len = 0.0
        self._signature: Tuple[float, int] = (0.0, 0)
        self._last_check = 0.0
        self.reload(force=True)

    # ---------------------------------------------------------- 加载与切块

    def _files(self) -> List[Path]:
        if not self.root.is_dir():
            return []
        files = [p for p in self.root.rglob("*") if p.suffix.lower() in PATHS_EXT and p.is_file()]
        return sorted(files, key=lambda p: str(p).lower())

    def _signature_of(self, files: Sequence[Path]) -> Tuple[float, int]:
        newest = 0.0
        for p in files:
            try:
                newest = max(newest, p.stat().st_mtime)
            except OSError:
                pass
        return (newest, len(files))

    def maybe_reload(self, interval: float = 5.0) -> bool:
        now = time.time()
        if now - self._last_check < interval:
            return False
        self._last_check = now
        files = self._files()
        if self._signature_of(files) != self._signature:
            self.reload(force=True, files=files)
            return True
        return False

    def reload(self, force: bool = False, files: Optional[Sequence[Path]] = None) -> None:
        files = list(files) if files is not None else self._files()
        self._signature = self._signature_of(files)
        chunks: List[Chunk] = []
        for path in files:
            try:
                raw = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            chunks.extend(self._split(path, raw))
        for c in chunks:
            c.tokens = tokenize(f"{c.heading} {c.text}")
            for t in c.tokens:
                c.tf[t] = c.tf.get(t, 0) + 1
            c.bigrams = _cjk_bigrams(f"{c.heading} {c.text}")
        self.chunks = chunks
        df: Dict[str, int] = {}
        for c in chunks:
            for t in set(c.tf):
                df[t] = df.get(t, 0) + 1
        self._df = df
        self._avg_len = (sum(len(c.tokens) for c in chunks) / len(chunks)) if chunks else 0.0

    def _split(self, path: Path, raw: str) -> List[Chunk]:
        """按小节切：一个小节尽量整块保留，超长才按段落拆。

        之前按空行切会让一条 bullet 变成一个片段（平均只有 17 个词），
        BM25 的文档长度归一化直接把分数压到门槛以下，该命中的都漏了。
        """
        chunks: List[Chunk] = []
        heading = ""
        buf: List[str] = []
        buf_len = 0

        def flush() -> None:
            nonlocal buf, buf_len
            text = _norm("".join(buf))
            if text:
                chunks.append(Chunk(source=path.stem, heading=heading, text=text))
            buf = []
            buf_len = 0

        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                flush()
                heading = stripped.lstrip("#").strip()
                continue
            if not stripped:
                # 空行只在缓冲区已经够长时才断开，避免小节被切碎
                if buf_len >= self.chunk_size:
                    flush()
                continue
            if buf_len and buf_len + len(stripped) > self.chunk_size * 1.6:
                flush()
            buf.append(stripped + "\n")
            buf_len += len(stripped)
        flush()
        return chunks

    # ---------------------------------------------------------- 检索

    def search(self, query: str, top_k: int = 3, min_score: float = 2.0
               ) -> List[Tuple[float, Chunk]]:
        if not self.chunks or not query.strip():
            return []
        q_tokens = tokenize(query)
        q_bigrams = _cjk_bigrams(query)
        if not q_tokens and not q_bigrams:
            return []
        n = len(self.chunks)
        k1, b = 1.5, 0.75
        scored: List[Tuple[float, Chunk]] = []
        for c in self.chunks:
            score = 0.0
            length = len(c.tokens) or 1
            for t in q_tokens:
                f = c.tf.get(t, 0)
                if not f:
                    continue
                df = self._df.get(t, 0) or 0
                if df <= 0:
                    continue
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                denom = f + k1 * (1 - b + b * length / (self._avg_len or 1))
                score += idf * f * (k1 + 1) / denom
            # 字二元组覆盖率：兜住分词切不出来的口语问法
            if q_bigrams:
                cover = len(q_bigrams & c.bigrams) / len(q_bigrams)
                if cover >= 0.34:
                    score += cover * 4.0
            if score <= 0:
                continue
            # 整段原句命中额外加权（短问题里最常见、也最可信的信号）
            for t in {query.strip(), _norm(query)}:
                if len(t) >= 3 and t in c.text:
                    score += 3.0
                    break
            if score >= min_score:
                scored.append((score, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

    def context_for(self, query: str, top_k: int = 3, min_score: float = 2.0,
                    max_chars: int = 900) -> str:
        hits = self.search(query, top_k=top_k, min_score=min_score)
        if not hits:
            return ""
        parts: List[str] = []
        used = 0
        for score, c in hits:
            block = c.render()
            if used + len(block) > max_chars and parts:
                break
            parts.append(block)
            used += len(block)
        return "\n\n".join(parts)

    # ---------------------------------------------------------- 诊断

    def stats(self) -> Dict[str, object]:
        return {
            "files": len({c.source for c in self.chunks}),
            "chunks": len(self.chunks),
            "avg_len": round(self._avg_len, 1),
            "vocab": len(self._df),
        }
