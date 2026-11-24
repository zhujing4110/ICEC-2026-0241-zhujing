# -*- coding: utf-8 -*-
"""他发来的非文本消息：图片、表情包、语音、视频、文件、链接。

以前 `handle_chat` 只认 ``type == "文本"``，你发图、发贴纸她根本看不见 ——
不是不回，是消息在过滤那一行就被扔了，水位还照常推进，等于永久丢失。
这个模块把非文本消息翻译成她读得懂的一行字：

    [他发来一张图片]
    [他发来一个表情包：无语]
    [他发来一个文件：作品集.pdf]

关于「看见」的边界，先说清楚，免得自己骗自己：
- 环境里只有文本模型（deepseek-flash / deepseek-v4-pro），**没有视觉模型**。
  所以她知道"他发了个表情包"，但看不到里面画的是什么。
- 表情包原图能从微信 CDN 直接拉下来（明文 GIF，XML 里的 md5 就是原图 md5），
  落地后算平均哈希，和她自己表情库里的图比对：同一张图就能读出标签。
  md5 精确命中优先，其次 aHash 汉明距离 <= 8。
- 图片消息只读本机 .dat 解密（不驱动界面），拿不到就只留描述。
- 你在控制台给某个 md5 写一句「这是狗头，意思是反讽」，之后同款她就认得。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image

from wechatauto.logger import wxlog

Image.MAX_IMAGE_PIXELS = None
try:
    Image.LOAD_TRUNCATED_IMAGES = True   # CDN 截断时也能解出首帧
except Exception:  # noqa: BLE001
    pass

MAX_SIDE = 320
AHASH_MAX_DISTANCE = 8          # 同一张图缩放/转码后 aHash 一般只差 0-4 位
FETCH_TIMEOUT = 12
FETCH_MAX_BYTES = 8 * 1024 * 1024

# 表情包这类"一眼就该有反应"的，头像/系统消息这类不该回
RESPOND_TYPES = {"图片", "动画表情", "语音", "视频", "文件/链接/卡片",
                 "位置", "红包", "名片", "音乐"}
IGNORE_TYPES = {"系统消息", "音视频通话"}


def _fields(xml_text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k, v in re.findall(r'([A-Za-z_][\w:.-]*)\s*=\s*"([^"]*)"', xml_text or ""):
        out.setdefault(k, v)
    return out


def _child_text(xml_text: str, tag: str) -> str:
    """取子元素的文本：<title>标题</title>。卡片消息的标题是子元素不是属性。"""
    m = re.search(r"<%s(?:\s[^>]*)?>(.*?)</%s>" % (tag, tag), xml_text or "", re.S)
    if not m:
        return ""
    return _unescape_text(m.group(1))


def _unescape_text(s: str) -> str:
    return (s or "").replace("&amp;", "&").replace("&lt;", "<").replace(
        "&gt;", ">").replace("&quot;", '"').replace("&#39;", "'").strip()


def _root_tag(xml_text: str) -> str:
    m = re.search(r"<\s*([A-Za-z_][\w:.-]*)", xml_text or "")
    return (m.group(1).lower() if m else "")


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _unescape_url(u: str) -> str:
    return (u or "").replace("&amp;", "&").replace("&#38;", "&")


def _ahash(img: Image.Image) -> int:
    """8×8 平均哈希，和 stickers.normalize 里算的是同一套，才能互相比。"""
    g = img.convert("L").resize((8, 8), Image.LANCZOS)
    px = list(g.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for i, p in enumerate(px):
        if p > avg:
            bits |= 1 << i
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _shrink(raw: bytes) -> Optional[tuple]:
    """解码首帧并缩到 320px。返回 (png 字节, 宽, 高, aHash)。"""
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:  # noqa: BLE001
        try:
            # 动图/截断：只取第一帧
            img = Image.open(io.BytesIO(raw))
            img.seek(0)
            img.load()
        except Exception:  # noqa: BLE001
            return None
    w, h = img.size
    if min(w, h) < 8 or max(w, h) > 20000:
        return None
    img = img.convert("RGBA") if img.mode in ("RGBA", "LA", "P", "PA") else img.convert("RGB")
    hashed = _ahash(img)
    if max(w, h) > MAX_SIDE:
        s = MAX_SIDE / max(w, h)
        img = img.resize((max(1, int(w * s)), max(1, int(h * s))), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue(), w, h, hashed


class Inbox:
    """把非文本消息变成一行字，顺手把图落地成文件。"""

    def __init__(self, base: Path, stickers=None, media=None, db=None,
                 max_items: int = 60):
        self.base = Path(base)
        self.emoji_dir = self.base / "emojis"
        self.image_dir = self.base / "images"
        for d in (self.base, self.emoji_dir, self.image_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.stickers = stickers
        self.media = media
        self.db = db
        self.max_items = max_items
        self._lock = threading.Lock()
        self.known = self._load_json("known.json", {})
        self.items: List[Dict[str, Any]] = self._load_json("items.json", [])

    # -- 落盘 --------------------------------------------------------
    def _load_json(self, name: str, default):
        p = self.base / name
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return d if isinstance(d, type(default)) else default
        except Exception:  # noqa: BLE001
            return default

    def _save_json(self, name: str, data) -> None:
        p = self.base / name
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, p)
        except Exception as exc:  # noqa: BLE001
            wxlog.warning("写 %s 失败: %s", name, exc)

    def _remember(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            self.items.insert(0, rec)
            if len(self.items) > self.max_items:
                del self.items[self.max_items:]
            self._save_json("items.json", self.items)

    # -- 认识这张图 --------------------------------------------------
    def _sticker_label(self, md5: str, path: Optional[str], ahash: Optional[int]) -> str:
        if md5 and md5 in self.known:
            return str(self.known[md5]).strip()
        if self.stickers is None:
            return ""
        try:
            for it in self.stickers.all():
                if md5 and it.get("md5") == md5:
                    tags = it.get("tags") or []
                    if tags:
                        return str(tags[0])
        except Exception:  # noqa: BLE001
            pass
        if ahash is None:
            return ""
        try:
            best, dist = None, 999
            for it in self.stickers.all():
                h = it.get("hash")
                if not isinstance(h, int):
                    continue
                d = _hamming(ahash, h)
                if d < dist:
                    best, dist = it, d
            if best is not None and dist <= AHASH_MAX_DISTANCE:
                tags = best.get("tags") or []
                if tags:
                    return str(tags[0])
        except Exception:  # noqa: BLE001
            pass
        return ""

    def note(self, md5: str, text: str) -> bool:
        """你告诉她这张表情是什么意思，以后同款她就认得。"""
        md5 = (md5 or "").strip()
        text = (text or "").strip()[:40]
        if not md5 or not text:
            return False
        with self._lock:
            self.known[md5] = text
            self._save_json("known.json", self.known)
        return True

    def forget_note(self, md5: str) -> bool:
        with self._lock:
            if self.known.pop(md5, None) is None:
                return False
            self._save_json("known.json", self.known)
        return True

    # -- 表情包 ------------------------------------------------------
    def _save_emoji(self, md5: str, cdnurl: str) -> tuple:
        """从 CDN 拉原图，存首帧 PNG。返回 (路径, 宽, 高, aHash)。"""
        path = self.emoji_dir / f"{md5}.png"
        if path.exists():
            try:
                img = Image.open(path)
                return str(path), img.size[0], img.size[1], _ahash(img)
            except Exception:  # noqa: BLE001
                pass
        if not cdnurl:
            return "", 0, 0, None
        url = _unescape_url(cdnurl)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MicroMessenger Client"})
            op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            raw = op.open(req, timeout=FETCH_TIMEOUT).read(FETCH_MAX_BYTES)
        except Exception as exc:  # noqa: BLE001
            wxlog.debug("表情原图下载失败 %s: %s", md5, exc)
            return "", 0, 0, None
        got = _shrink(raw)
        if not got:
            return "", 0, 0, None
        png, w, h, hsh = got
        try:
            path.write_bytes(png)
        except Exception as exc:  # noqa: BLE001
            wxlog.warning("存表情失败: %s", exc)
            return "", w, h, hsh
        return str(path), w, h, hsh

    # -- 图片 --------------------------------------------------------
    def _save_image(self, msg: Dict[str, Any], user: str) -> str:
        if self.media is None:
            return ""
        try:
            return self.media.download_image(
                user, int(msg.get("local_id") or 0), str(self.image_dir),
                tier="best", shard=msg.get("shard") or None,
            ) or ""
        except Exception as exc:  # noqa: BLE001
            wxlog.debug("图片解密失败 %s: %s", msg.get("local_id"), exc)
            return ""

    # -- 主入口 ------------------------------------------------------
    def accept(self, msg: Dict[str, Any], user: str = "") -> Optional[str]:
        """把一条非文本消息翻成她能读的一行；返回 None 表示这条不用理。"""
        mtype = str(msg.get("type") or "")
        if mtype in IGNORE_TYPES:
            return None
        content = msg.get("content") or ""
        tag = _root_tag(content)
        f = _fields(content)
        md5 = (f.get("md5") or "").lower()
        rec: Dict[str, Any] = {
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": mtype,
            "user": user,
            "local_id": msg.get("local_id"),
            "md5": md5,
            "file": "",
            "label": "",
            "line": "",
        }

        if mtype == "动画表情" or tag == "emoji":
            path, w, h, hsh = ("", 0, 0, None)
            if md5:
                path, w, h, hsh = self._save_emoji(md5, f.get("cdnurl") or "")
            label = self._sticker_label(md5, path or None, hsh)
            line = f"[他发来一个表情包：{label}]" if label else "[他发来一个表情包]"
            if label:
                wxlog.info("认出表情包 %s → %s", md5[:8], label)
            rec.update(file=path, label=label, line=line, kind="sticker")

        elif mtype == "图片" or tag == "img":
            path = self._save_image(msg, user)
            line = "[他发来一张图片]"
            rec.update(file=path, line=line, kind="image")

        elif mtype == "语音" or tag == "voicemsg":
            secs = _int(f.get("voicelength")) // 1000
            line = f"[他发来一条语音，{secs} 秒]" if 0 < secs < 600 else "[他发来一条语音]"
            rec.update(line=line, kind="voice")

        elif mtype == "视频":
            line = "[他发来一段视频]"
            rec.update(line=line, kind="video")

        elif mtype == "位置":
            line = "[他发来一个位置]"
            rec.update(line=line, kind="location")

        elif mtype == "红包":
            line = "[他发来一个红包]"
            rec.update(line=line, kind="hongbao")

        elif tag == "appmsg" or mtype == "文件/链接/卡片":
            title = _child_text(content, "title")[:60]
            url = _child_text(content, "url")
            sub = _int(_child_text(content, "type") or f.get("type"))
            if sub == 6:
                line = f"[他发来一个文件：{title}]" if title else "[他发来一个文件]"
                kind = "file"
            elif sub in (3, 4, 5) or url.startswith("http"):
                line = f"[他发来一个链接：{title}]" if title else "[他发来一个链接]"
                kind = "link"
            elif title:
                line = f"[他发来一张卡片：{title}]"
                kind = "card"
            else:
                line = "[他发来一张卡片]"
                kind = "card"
            rec.update(line=line, kind=kind)

        else:
            if mtype not in RESPOND_TYPES:
                return None
            line = f"[他发来一条{mtype}消息]"
            rec.update(line=line, kind="other")

        rec["line"] = line
        self._remember(rec)
        return line

    # -- 给控制台 ----------------------------------------------------
    def status(self) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        # 你在控制台补过注释的，列表里也要跟着显示「已认识」，
        # 否则教过一次还是写着「认不出这张」，看着像没生效
        items: List[Dict[str, Any]] = []
        with self._lock:
            for src in self.items:
                it = dict(src)
                md5 = it.get("md5") or ""
                note = str(self.known.get(md5) or "").strip()
                if note:
                    it["label"] = note
                items.append(it)
            for it in items:
                k = it.get("kind") or "other"
                counts[k] = counts.get(k, 0) + 1
            known = len(self.known)
        return {"count": len(items), "kinds": counts,
                "known": known, "items": items}

    def known_list(self) -> List[Dict[str, str]]:
        with self._lock:
            return [{"md5": k, "note": v} for k, v in self.known.items()]
