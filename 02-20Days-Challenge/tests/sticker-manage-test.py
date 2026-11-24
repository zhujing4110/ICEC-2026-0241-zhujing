# -*- coding: utf-8 -*-
"""验证表情包的手动管理：上传本地图、改标签、去重、标签能否被 pick 认出来。

用临时目录 + 临时端口跑真的 webui Handler，不碰真实表情库。
"""
import base64
import io
import json
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(BASE))

from PIL import Image
import stickers as S
import webui

PORT = 8799
TMP = Path(tempfile.mkdtemp(prefix="stk_"))


def png_bytes(color, size=(220, 220)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def call(path, payload=None):
    url = f"http://127.0.0.1:{PORT}{path}"
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    if payload is None:
        req = urllib.request.Request(url)
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
        )
    try:
        with op.open(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class FakeBridge:
    def __init__(self, book):
        self.stickers = book
        self.pending = {}
        self._collecting = False

    def sticker_status(self):
        # 跟 bridge.py 里的真实实现保持一致
        items = [{
            "file": it.get("file"), "tags": it.get("tags") or [],
            "uses": it.get("uses") or 0, "added_at": it.get("added_at", ""),
            "kb": int((it.get("bytes") or 0) / 1024),
        } for it in self.stickers.all()]
        items.sort(key=lambda x: x.get("added_at") or "", reverse=True)
        return {"enabled": True, "collecting": False,
                "stats": self.stickers.stats(), "items": items[:60]}


book = S.StickerBook(TMP / "stickers", max_items=50)
srv = webui.start(FakeBridge(book), "127.0.0.1", PORT)
assert srv, "webui 起不来"
time.sleep(0.5)

try:
    # 1. 上传三张不同的图
    url1 = "data:image/png;base64," + base64.b64encode(png_bytes((200, 30, 30))).decode()
    code, r = call("/api/stickers/upload", {"data": url1, "tags": "无语, 测试"})
    print("[1] 上传:", code, r.get("ok"), r.get("item", {}).get("tags"))
    assert code == 200 and r["ok"], r
    f1 = r["item"]["file"]
    assert (TMP / "stickers" / f1).exists(), "文件没落盘"

    code, r = call("/api/stickers/upload", {
        "data": "data:image/png;base64," + base64.b64encode(png_bytes((30, 200, 30))).decode(),
        "tags": "开心"})
    assert r["ok"], r

    # 2. 同一张再传一次 → 应该被去重拦掉
    code, r = call("/api/stickers/upload", {"data": url1, "tags": "重复"})
    print("[2] 重复上传:", code, r.get("reason"))
    assert code == 400 and "已经" in (r.get("reason") or ""), r

    # 3. 改标签
    code, r = call("/api/stickers/retag", {"file": f1, "tags": "累了，加班"})
    print("[3] 改标签:", code, r.get("tags"))
    assert code == 200 and r["tags"] == ["累了", "加班"], r

    # 4. 改完的标签必须能被 pick 认出来（这是改标签的意义）
    got = book.pick("累了")
    print("[4] 按新标签挑图:", got.get("file") if got else None)
    assert got and got["file"] == f1, "改完标签却挑不到"

    # 5. 空标签要被拒
    code, r = call("/api/stickers/retag", {"file": f1, "tags": "  ，  "})
    print("[5] 空标签:", code, r.get("reason"))
    assert code == 400, r

    # 6. 不存在的文件
    code, r = call("/api/stickers/retag", {"file": "../../etc/passwd", "tags": "x"})
    print("[6] 乱传文件名:", code, r.get("reason"))
    assert code == 400, r

    # 7. 非图片数据
    code, r = call("/api/stickers/upload", {
        "data": "data:image/png;base64," + base64.b64encode(b"not an image").decode(),
        "tags": "x"})
    print("[7] 非图片:", code, r.get("reason"))
    assert code == 400, r

    # 8. 列表中能读到新标签和使用次数
    code, d = call("/api/stickers")
    item = [x for x in d["items"] if x["file"] == f1][0]
    print("[8] 列表里的标签:", item["tags"], "| 总数:", d["stats"]["count"])
    assert item["tags"] == ["累了", "加班"]
    assert d["stats"]["count"] == 2

    print("\n全部通过")
finally:
    srv.shutdown()
    shutil.rmtree(TMP, ignore_errors=True)
