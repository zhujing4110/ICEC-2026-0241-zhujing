# -*- coding: utf-8 -*-
"""联网检索：Bing 抓取 + 网页正文提取，只依赖标准库。

设计原则：抓取失败一律返回空，绝不抛异常。
聊天场景里"搜不到"远好过"因为搜不到所以没回消息"。
"""
from __future__ import annotations

import base64
import re
import ssl
import urllib.parse
import urllib.request
from typing import Dict, List, Tuple

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 部分站点证书链不全，聊天机器人场景下不值得为此失败
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")
BLOCK_RE = re.compile(r'<li class="b_algo"(.*?)</li>', re.S)
H2_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
DROP_RE = re.compile(r"<(script|style|nav|footer|header|noscript|svg)[^>]*>.*?</\1>", re.S | re.I)
ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&#39;": "'", "&ldquo;": "“", "&rdquo;": "”",
    "&mdash;": "—", "&hellip;": "…",
}


def _get(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        raw = r.read()
        charset = ""
        m = re.search(r"charset=([\w-]+)", r.headers.get("Content-Type", ""), re.I)
        if m:
            charset = m.group(1)
        else:
            head = raw[:3000].decode("ascii", "ignore")
            m2 = re.search(r'charset=["\']?([\w-]+)', head, re.I)
            if m2:
                charset = m2.group(1)
    for enc in filter(None, [charset, "utf-8", "gb18030"]):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "ignore")


def _text(html: str) -> str:
    html = DROP_RE.sub(" ", html)
    html = TAG_RE.sub(" ", html)
    for k, v in ENTITIES.items():
        html = html.replace(k, v)
    # 剩下的命名实体和数字实体（&ensp; &#0183; 之类）一律当空格
    html = re.sub(r"&#\d+;?", " ", html)
    html = re.sub(r"&[a-zA-Z]{2,10};", " ", html)
    return WS_RE.sub(" ", html).strip()


def _unwrap(href: str) -> str:
    """Bing 有时把结果包成 /ck/a?...&u=a1<base64>，这里是还原真实地址。"""
    if not href.startswith(("/ck/a", "http://www.bing.com/ck/a", "https://www.bing.com/ck/a")):
        return href
    m = re.search(r"[?&]u=a1([^&]+)", href)
    if not m:
        return href
    raw = m.group(1)
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
    except Exception:
        return href


def search(query: str, top: int = 4, timeout: float = 8.0) -> List[Dict[str, str]]:
    url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query)
    try:
        html = _get(url, timeout)
    except Exception:
        return []
    if len(html) < 5000:  # 太短基本是被反爬拦了
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for block in BLOCK_RE.findall(html):
        m = H2_RE.search(block)
        if not m:
            continue
        href = _unwrap(m.group(1))
        title = _text(m.group(2))
        if not title or href in seen:
            continue
        snippet = WS_RE.sub(" ", " ".join(_text(p) for p in P_RE.findall(block))).strip()
        out.append({"title": title, "url": href, "snippet": snippet[:300]})
        seen.add(href)
        if len(out) >= top:
            break
    return out


def fetch_text(url: str, max_chars: int = 600, timeout: float = 8.0) -> str:
    try:
        html = _get(url, timeout)
    except Exception:
        return ""
    paras = [_text(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", html, re.S)]
    paras = [p for p in paras if len(p) >= 15]
    text = " ".join(paras)
    if len(text) < 40:
        text = _text(html)
    return text[:max_chars]


def search_and_read(query: str, top: int = 4, read_top: int = 1,
                    max_chars: int = 600, timeout: float = 8.0) -> Tuple[List[Dict[str, str]], str]:
    results = search(query, top=top, timeout=timeout)
    extra = ""
    if results and read_top > 0:
        extra = fetch_text(results[0]["url"], max_chars=max_chars, timeout=timeout)
    return results, extra


def format_for_prompt(results: List[Dict[str, str]], extra: str = "") -> str:
    if not results:
        return ""
    lines = [f"{i}. {r['title']}：{r['snippet'][:160]}" for i, r in enumerate(results, 1)]
    body = "\n".join(lines)
    if extra:
        body += "\n\n第一条网页的正文摘录：\n" + extra[:500]
    return body


# ---------------------------------------------------------------- 天气

# 从「深圳今天天气怎么样」里把城市名抠出来
CITY_NOISE = re.compile(
    r"(今天|明天|后天|大后天|现在|实时|当前|天气|气温|温度|多少度|几度|怎么样|如何|会|要|"
    r"下雨|下雪|冷不冷|热不热|预报|情况|的|吗|呢|呀|啊|吧|？|\?|，|,|。|\s)+"
)


def city_from_query(q: str) -> str:
    return CITY_NOISE.sub("", q or "").strip()


def weather(city: str = "", timeout: float = 8.0) -> str:
    """查实时天气。city 留空则按出口 IP 定位。

    搜索引擎的摘要里通常只有"XX天气网提供预报"这种导航文字，拿不到温度，
    所以天气单独走 wttr.in，直接返回结构化数值。
    """
    fmt = "%l %c %t 体感%f 湿度%h"
    url = ("https://wttr.in/" + urllib.parse.quote(city.strip()) +
           "?format=" + urllib.parse.quote(fmt) + "&lang=zh")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
            txt = r.read(400).decode("utf-8", "ignore")
    except Exception:
        return ""
    txt = WS_RE.sub(" ", txt.replace("+", " ").replace("\n", " ")).strip()
    if not txt or "<" in txt or any(k in txt for k in ("Unknown location", "ERROR", "Sorry")):
        return ""
    return txt[:120]
