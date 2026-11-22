#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""知识库调试工具：不用发微信，直接看你问一句能命中什么。

用法：
    .venv\\Scripts\\python.exe kb-test.py "你能帮我做海报吗"
    .venv\\Scripts\\python.exe kb-test.py "怎么收费" --raw     # 不看门槛，列出所有候选打分
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from knowledge import KnowledgeBase  # noqa: E402

BASE = Path(__file__).resolve().parent


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    raw = "--raw" in sys.argv
    if not args:
        print(__doc__)
        return 1
    query = args[0]

    cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
    kcfg = cfg.get("knowledge", {})
    kb = KnowledgeBase(BASE / kcfg.get("dir", "knowledge"))

    st = kb.stats()
    print(f"知识库：{st['files']} 个文件 / {st['chunks']} 个片段 / 词表 {st['vocab']}")
    print(f"提问：{query}")
    print("-" * 60)

    hits = kb.search(
        query,
        top_k=10 if raw else kcfg.get("top_k", 3),
        min_score=0.0 if raw else kcfg.get("min_score", 1.2),
    )
    if not hits:
        print("没有命中。闲聊类问题这是正常结果；")
        print("如果这是应该能答的问题，说明知识库里没写这件事，或写法和问法差太远。")
        return 0

    for score, c in hits:
        print(f"[{score:.2f}] {c.source} · {c.heading}")
        print("   " + c.text[:160].replace("\n", " "))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
