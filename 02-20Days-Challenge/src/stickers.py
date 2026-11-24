"""表情包：自己上网搜图、下载、压缩、收进小仓库，聊天时按情绪挑一张发出去。

图源以 360 图片为主（Bing 图片对长中文查询切词很差，只做兜底）。

设计取向：
- 只要"能当表情包用"的图：方形或近方形、不太小、能解码。图搜结果里混着
  新闻配图和视频封面，这些形状和内容都不像表情包，靠尺寸/长宽比就能筛掉大半。
- 存 PNG 并压到 320px 以内：微信粘贴发图会先把文件塞进剪贴板，几百 KB 的图
  粘贴和发送都慢，还会拖慢整轮回复。
- 用平均哈希去重：同一张梗图常有好几个域名转载，光比 md5 拦不住。
"""
from __future__ import annotations

import hashlib
import io
import json
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

IMG_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp")
MAX_BYTES = 3 * 1024 * 1024      # 单张原图上限，超过基本是壁纸
MIN_BYTES = 8 * 1024             # 太小多是图标/占位图
MIN_SIDE = 80
MAX_SIDE = 320                   # 存下来最长边
MURL_RE = re.compile(r'murl&quot;:&quot;(.*?)&quot;')
MURL_RE2 = re.compile(r'"murl":"(.*?)"')
TAG_RE = re.compile(r"[^\w\u4e00-\u9fff]+")


def _unescape(url: str) -> str:
    return (url.replace("&amp;", "&").replace("\\/", "/")
               .replace("\\u0026", "&").strip())


def _get(url: str, timeout: int = 8, referer: str = "") -> Optional[bytes]:
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        headers["Referer"] = referer
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:  # noqa: BLE001
        return None


def _search_360(query: str, limit: int) -> List[Dict[str, str]]:
    """360 图片的 JSON 接口。中文切词比 Bing 靠谱得多，"无语"不会搜成"无"。"""
    url = ("https://image.so.com/j?q=%s&src=srp&correct=%s&sn=0&pn=%d"
           % (urllib.parse.quote(query), urllib.parse.quote(query), max(limit * 3, 20)))
    raw = _get(url, timeout=8, referer="https://image.so.com/")
    if not raw:
        return []
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for it in data.get("list") or []:
        title = re.sub(r"<[^>]+>", "", str(it.get("title") or ""))[:120]
        for key in ("img", "thumb"):
            u = str(it.get(key) or "").strip()
            if not u.startswith("http") or u in seen:
                continue
            seen.add(u)
            out.append({"url": u, "title": title})
            break
        if len(out) >= limit:
            break
    return out


def _search_bing(query: str, limit: int) -> List[Dict[str, str]]:
    url = ("https://cn.bing.com/images/search?q=" + urllib.parse.quote(query)
           + "&qft=+filterui:imagesize-medium&form=IRFLTR")
    raw = _get(url, timeout=8)
    if not raw:
        return []
    html = raw.decode("utf-8", "ignore")
    found = MURL_RE.findall(html) or MURL_RE2.findall(html)
    out: List[Dict[str, str]] = []
    seen = set()
    for u in found:
        u = _unescape(u)
        if not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        out.append({"url": u, "title": ""})
        if len(out) >= limit:
            break
    return out


def search_images(query: str, limit: int = 12) -> List[Dict[str, str]]:
    """搜图，返回 [{url, title, src}]。360 为主，不够再问 Bing。"""
    out = _search_360(query, limit)
    for x in out:
        x["src"] = "360"
    if len(out) < limit:
        rest = _search_bing(query, limit - len(out))
        for x in rest:
            x["src"] = "bing"
        out += rest
    return out


def search_urls(query: str, limit: int = 12) -> List[str]:
    return [x["url"] for x in search_images(query, limit)]


def title_matches(title: str, keyword: str) -> bool:
    """标题里得真的有关键词，否则宁可不要。

    "无语" 会搜出 "无所谓"、"无" 字字典页，所以按整词比，不按单字比。
    留一个例外：搜 "猫 表情" 时标题可能只写 "猫咪搞笑图"，所以退一步比 2 字前缀。
    """
    if not title:
        return False
    key = re.sub(r"表情包?|图片|微信|大全|合集|高清|素材|搞笑|沙雕|可爱", "", keyword)
    key = key.strip()
    if not key:
        return True
    if key in title:
        return True
    for part in re.split(r"[\s,，、]+", key):
        if len(part) >= 2 and part in title:
            return True
        if len(part) == 1 and part in title:
            return True
    return False


def fetch_image(url: str, timeout: int = 8) -> Optional[bytes]:
    """下载一张图。体积、类型都过一遍，不合格就丢。"""
    data = _get(url, timeout=timeout, referer="https://image.so.com/")
    if not data:
        return None
    if not (MIN_BYTES <= len(data) <= MAX_BYTES):
        return None
    return data


def normalize(data: bytes) -> Optional[Tuple[Image.Image, int]]:
    """解出图片并裁到 320px 内。返回 (图, 平均哈希)。

    长度比超过 2.6 或小于 0.38 的直接丢 —— 那是横幅和长截图，贴出来像发错东西。
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:  # noqa: BLE001
        return None

    w, h = img.size
    if min(w, h) < MIN_SIDE:
        return None
    ratio = w / max(h, 1)
    if ratio > 2.6 or ratio < 0.38:
        return None

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
    else:
        img = img.convert("RGB")

    if max(w, h) > MAX_SIDE:
        scale = MAX_SIDE / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                         Image.LANCZOS)

    hash_img = img.convert("L").resize((8, 8), Image.LANCZOS)
    px = list(hash_img.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for i, p in enumerate(px):
        if p > avg:
            bits |= 1 << i
    return img, bits


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def tags_of(query: str) -> List[str]:
    """把搜索词切成标签，供聊天时按情绪匹配。"""
    raw = [t for t in TAG_RE.split(query) if t]
    drop = {"表情包", "表情", "微信", "可爱", "图片", "高清", "素材", "大全"}
    return [t for t in raw if t not in drop and len(t) >= 1][:6]


# 模型给的情绪词和库里的标签常常不是同一个说法（"喜滋滋" vs "开心"），
# 不兜一层的话，图明明有，却因为一个词对不上就发不出去，看上去就像"她不爱发图"。
STICKER_SYNONYMS = {
    "开心": ("高兴", "喜滋滋", "美滋滋", "爽", "兴奋", "乐", "偷笑", "得意",
             "开森", "笑死", "开心死了"),
    "累了": ("困", "疲惫", "没力气", "累死", "倦", "乏", "虚", "撑不住", "好累", "累趴"),
    "无语": ("无奈", "烦", "烦躁", "郁闷", "汗", "服了", "麻了", "离谱", "沉默",
             "扶额", "醉了", "服气"),
    "疑惑": ("问号", "不懂", "迷惑", "懵", "困惑", "莫名其妙", "啥", "啊这"),
    "尴尬": ("社死", "尬", "难为情", "不好意思", "汗颜"),
    "生气": ("火大", "气死", "怒", "恼", "来气", "火"),
    "加油": ("打气", "挺住", "冲", "鼓励", "支持", "顶住"),
    "晚安": ("睡了", "睡觉", "困觉", "夜里", "晚安啦", "该睡了"),
    "猫": ("喵", "猫咪", "小猫咪"),
    "咖啡": ("美式", "拿铁", "喝咖啡"),
    "画图": ("画画", "作图", "设计图"),
    # 下面这些是库里实际出现过的口语标签，模型换种说法时也能兜到
    "不开心": ("郁闷", "烦", "烦躁", "丧", "低落", "难受", "不爽", "心情不好", "emo"),
    "震惊": ("吃惊", "卧槽", "天哪", "吓到", "不敢信", "离谱死了"),
    "疑问": ("不懂", "困惑", "问号脸", "什么鬼"),
    "干嘛": ("在吗", "干啥", "什么情况", "怎么了呀", "搞嘛"),
    "好样的": ("牛逼", "厉害", "绝了", "佩服", "太强"),
    "穷": ("没钱", "吃土", "破产", "穷死"),
    "怎么这样": ("过分", "什么鬼东西", "过分了", "太离谱"),
}


_NEG_HEADS = ("不", "没", "别")


def _same_polarity(a: str, b: str) -> bool:
    """「不开心」和「开心」不是一回事，别让子串匹配把情绪搞反。"""
    neg_a = any(a.startswith(n) for n in _NEG_HEADS)
    neg_b = any(b.startswith(n) for n in _NEG_HEADS)
    return neg_a == neg_b


def canonical_tags(kw: str) -> List[str]:
    """把模型给的情绪词扩展成库里可能存在的标签。"""
    kw = (kw or "").strip()
    if not kw:
        return []
    out: List[str] = [kw]
    for canon, alts in STICKER_SYNONYMS.items():
        if canon in kw or any(a in kw or kw in a for a in alts):
            if canon not in out:
                out.append(canon)
    return out


class StickerBook:
    """表情包仓库。一人一份 index.json，文件平铺在同目录。"""

    def __init__(self, base: Path, max_items: int = 160):
        self.dir = Path(base)
        self.index_path = self.dir / "index.json"
        self.max_items = max_items
        self._lock = threading.Lock()
        self.dir.mkdir(parents=True, exist_ok=True)

    # -- 存储 --------------------------------------------------------
    def _read(self) -> Dict[str, Any]:
        if not self.index_path.exists():
            return {"items": [], "want": [], "meta": {}}
        try:
            d = json.loads(self.index_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"items": [], "want": [], "meta": {}}
        d.setdefault("items", [])
        d.setdefault("want", [])
        d.setdefault("meta", {})
        return d

    def _write(self, d: Dict[str, Any]) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.index_path)

    def all(self) -> List[Dict[str, Any]]:
        return self._read()["items"]

    def stats(self) -> Dict[str, Any]:
        items = self.all()
        tags: Dict[str, int] = {}
        for it in items:
            for t in it.get("tags") or []:
                tags[t] = tags.get(t, 0) + 1
        return {
            "count": len(items),
            "size_mb": round(sum(it.get("bytes") or 0 for it in items) / 1048576, 2),
            "tags": dict(sorted(tags.items(), key=lambda kv: -kv[1])[:12]),
            "wanted": len(self._read()["want"]),
        }

    # -- 收集 --------------------------------------------------------
    def add_query(self, query: str, count: int = 3) -> Dict[str, Any]:
        """搜一个词，尽量存下 count 张。传入口语词即可（"无语"），内部会补成可搜的形式。"""
        query = query_for(query)
        results = search_images(query, limit=count * 5)
        # 标题里带"表情/表情包"的通常就是表情包站点，优先取；游戏截图、新闻配图排在后面
        results.sort(key=lambda r: 0 if "表情" in (r.get("title") or "") else 1)

        if not results:
            return {"added": 0, "tried": 0, "query": query, "reason": "搜索没有结果"}

        data = self._read()
        existing = [int(it.get("hash") or 0) for it in data["items"] if it.get("hash")]
        known_md5 = {it.get("md5") for it in data["items"]}
        tags = tags_of(query)
        added: List[Dict[str, Any]] = []

        for res in results:
            if len(added) >= count:
                break
            if len(data["items"]) >= self.max_items:
                break
            if res.get("src") == "360" and not title_matches(res.get("title") or "", query):
                continue
            raw = fetch_image(res["url"])
            if not raw:
                continue
            md5 = hashlib.md5(raw).hexdigest()
            if md5 in known_md5:
                continue
            got = normalize(raw)
            if not got:
                continue
            img, ah = got
            if any(hamming(ah, h) <= 4 for h in existing):
                continue

            name = f"st_{int(time.time())}_{md5[:6]}.png"
            path = self.dir / name
            try:
                img.save(path, "PNG", optimize=True)
            except Exception:  # noqa: BLE001
                continue

            item = {
                "file": name,
                "tags": tags,
                "query": query,
                "hash": ah,
                "md5": md5,
                "w": img.size[0],
                "h": img.size[1],
                "bytes": path.stat().st_size,
                "added_at": time.strftime("%Y-%m-%d %H:%M"),
                "uses": 0,
                "source": res["url"][:200],
            }
            data["items"].append(item)
            existing.append(ah)
            known_md5.add(md5)
            added.append(item)

        if added:
            self._write(data)
        return {"added": len(added), "tried": len(results), "query": query,
                "files": [a["file"] for a in added]}

    # -- 手动管理 ----------------------------------------------------
    def add_local_data(self, raw: bytes, tags: List[str]) -> Dict[str, Any]:
        """把一张本机的图收进库里。自己存的、别人发的、自己画的都行。"""
        if not raw:
            return {"ok": False, "reason": "文件是空的"}
        if len(raw) > 8 * 1048576:
            return {"ok": False, "reason": "图片太大（上限 8 MB）"}
        got = normalize(raw)
        if not got:
            return {"ok": False, "reason": "不是能识别的图片格式"}
        img, ah = got

        clean: List[str] = []
        for t in tags or []:
            t = str(t).strip()[:12]
            if t and t not in clean:
                clean.append(t)
        clean = clean[:4] or ["其他"]

        with self._lock:
            data = self._read()
            md5 = hashlib.md5(raw).hexdigest()
            if any(it.get("md5") == md5 for it in data["items"]):
                return {"ok": False, "reason": "这张已经在库里了"}
            if any(hamming(ah, int(it.get("hash") or 0)) <= 4
                   for it in data["items"] if it.get("hash")):
                return {"ok": False, "reason": "和库里某张几乎一模一样"}
            if len(data["items"]) >= self.max_items:
                return {"ok": False, "reason": f"库里满了（{self.max_items} 张），先删几张"}

            name = f"st_{int(time.time())}_{md5[:6]}.png"
            path = self.dir / name
            try:
                img.save(path, "PNG", optimize=True)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "reason": f"存不下来：{exc}"}

            item = {
                "file": name,
                "tags": clean,
                "query": "手动导入",
                "hash": ah,
                "md5": md5,
                "w": img.size[0],
                "h": img.size[1],
                "bytes": path.stat().st_size,
                "added_at": time.strftime("%Y-%m-%d %H:%M"),
                "uses": 0,
                "source": "local",
            }
            data["items"].append(item)
            self._write(data)
        return {"ok": True, "item": item}

    def retag(self, name: str, tags: List[str]) -> Dict[str, Any]:
        """改标签。pick() 全靠 tags 找图，标签写错等于这张永远发不出去。"""
        clean: List[str] = []
        for t in tags or []:
            t = str(t).strip()[:12]
            if t and t not in clean:
                clean.append(t)
        if not clean:
            return {"ok": False, "reason": "至少要留一个标签"}
        with self._lock:
            data = self._read()
            for it in data["items"]:
                if it.get("file") == name:
                    it["tags"] = clean[:4]
                    self._write(data)
                    return {"ok": True, "file": name, "tags": it["tags"]}
        return {"ok": False, "reason": "库里没有这张"}

    # -- 挑选 --------------------------------------------------------
    def pick(self, keyword: str = "") -> Optional[Dict[str, Any]]:
        """按情绪词挑一张。找不到就返回 None —— 宁可这次不发，也不发错情绪的图。"""
        items = self.all()
        if not items:
            return None
        kw = (keyword or "").strip()
        if not kw:
            pool = items
        else:
            # "喜滋滋" 也要能挑到标签是「开心」的图
            cands = canonical_tags(kw)
            scored = []
            for it in items:
                score = 0
                for t in it.get("tags") or []:
                    t = str(t)
                    if any(t == c for c in cands):
                        score += 5                      # 一模一样的词，最可信
                    elif any(_same_polarity(t, c) and (t in c or c in t) for c in cands):
                        score += 2                      # 一方是另一方的一部分
                    elif any(ch in kw for ch in t if "\u4e00" <= ch <= "\u9fff"):
                        score += 1
                if score:
                    scored.append((score, it))
            if not scored:
                return None
            top = max(s for s, _ in scored)
            pool = [it for s, it in scored if s == top]

        # 同一分数里优先用没发过的，避免总发同一张
        pool = sorted(pool, key=lambda x: (x.get("uses") or 0, random.random()))
        return pool[0]

    def mark_used(self, item: Dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            for it in data["items"]:
                if it.get("file") == item.get("file"):
                    it["uses"] = int(it.get("uses") or 0) + 1
                    it["last_used"] = time.strftime("%Y-%m-%d %H:%M")
                    break
            self._write(data)

    def want(self, keyword: str) -> None:
        """聊天时想发某个情绪但没有对应图，记下来，下次自动去收。"""
        kw = (keyword or "").strip()
        if not kw:
            return
        with self._lock:
            data = self._read()
            if kw not in data["want"]:
                data["want"] = (data["want"] + [kw])[-12:]
                self._write(data)

    def pop_want(self) -> Optional[str]:
        with self._lock:
            data = self._read()
            if not data["want"]:
                return None
            kw = data["want"].pop(0)
            self._write(data)
            return kw

    def path_of(self, name: str) -> Optional[Path]:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            return None
        p = (self.dir / name).resolve()
        if p.parent != self.dir.resolve() or not p.exists():
            return None
        return p

    def delete(self, name: str) -> bool:
        p = self.path_of(name)
        if not p:
            return False
        with self._lock:
            data = self._read()
            before = len(data["items"])
            data["items"] = [it for it in data["items"] if it.get("file") != name]
            self._write(data)
        try:
            p.unlink()
        except Exception:  # noqa: BLE001
            pass
        return len(data["items"]) != before


def query_for(keyword: str) -> str:
    """把情绪词/兴趣词变成一条能搜到表情包的查询。

    实测教训：加"表情包 微信"反而把结果带到"微信表情包新闻"文章页，
    标题全都不含关键词。只加"表情"两个字命中率最高（"无语 表情" 9/10 标题相关）。
    """
    k = keyword.strip()
    if not k:
        return ""
    if "表情" in k:
        return k
    return f"{k} 表情"


def collect_keywords(interests: str, emotions: List[str]) -> List[str]:
    """收集顺序：先把缺的情绪补齐，再按兴趣慢慢攒。"""
    out: List[str] = []
    for e in emotions or []:
        if e.strip():
            out.append(e.strip())
    for part in re.split(r"[、,，;；\s]+", interests or ""):
        if part.strip():
            out.append(part.strip())
    return out
