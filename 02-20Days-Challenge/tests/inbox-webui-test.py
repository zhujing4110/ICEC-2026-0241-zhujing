# -*- coding: utf-8 -*-
"""验证收件箱的控制台部分：列表、看缩略图、教她认图、越权路径。
临时目录 + 临时端口，不碰真实 inbox。
"""
import io
import json
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(BASE))

from PIL import Image
import webui
from inbox import Inbox

PORT = 8802
TMP = Path(tempfile.mkdtemp(prefix="ibx_"))


def call(path, payload=None, raw=False):
    url = f"http://127.0.0.1:{PORT}{path}"
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url) if payload is None else urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with op.open(req, timeout=10) as r:
            body = r.read()
            return r.status, (body if raw else json.loads(body.decode()))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body.decode())
        except Exception:  # noqa: BLE001
            return e.code, body


class FakeBridge:
    def __init__(self, box):
        self.inbox = box
        self.pending = {}


box = Inbox(TMP / "inbox", media=None, db=None)

# 造一张真实落地图 + 两条记录（一条表情、一条语音）
png = io.BytesIO()
Image.new("RGB", (120, 120), (30, 120, 200)).save(png, "PNG")
(box.emoji_dir / "abc123.png").write_bytes(png.getvalue())
box._remember({"at": "2026-11-24 16:20:01", "type": "动画表情", "user": "wxid_demo",
               "local_id": 1, "md5": "abc123def", "file": str(box.emoji_dir / "abc123.png"),
               "label": "", "line": "[他发来一个表情包]", "kind": "sticker"})
box._remember({"at": "2026-11-24 16:19:02", "type": "语音", "user": "wxid_demo",
               "local_id": 2, "md5": "", "file": "", "label": "",
               "line": "[他发来一条语音，3 秒]", "kind": "voice"})

srv = webui.start(FakeBridge(box), "127.0.0.1", PORT)
assert srv, "webui 起不来"
time.sleep(0.5)

try:
    # [1] 列表
    code, d = call("/api/inbox")
    print("[1] 列表:", code, "条数", d.get("count"), "类型", d.get("kinds"), "认识", d.get("known"))
    assert code == 200 and d["enabled"] and d["count"] == 2, d

    # [2] 缩略图能取到，且是 PNG
    code, body = call("/inbox/abc123.png", raw=True)
    print("[2] 缩略图:", code, len(body), "字节, PNG 头:", body[:4] == b"\x89PNG")
    assert code == 200 and body[:4] == b"\x89PNG"

    # [3] 越权：不能读到 inbox 外面的文件
    for bad in ("/inbox/../config.json", "/inbox/..%2Fconfig.json", "/inbox/../../persona.md"):
        code, _ = call(bad, raw=True)
        print("   越权试探", bad, "→", code)
        assert code == 404, "越权没挡住: %s" % bad

    # [4] 教她认识这张
    code, r = call("/api/inbox/note", {"md5": "abc123def", "note": "狗头，反讽"})
    print("[4] 写注释:", code, r)
    assert code == 200 and r["ok"]
    code, d = call("/api/inbox")
    item = next(x for x in d["items"] if x["md5"] == "abc123def")
    assert item["label"] == "狗头，反讽", item
    assert d["known"] == 1

    # [5] 再发同款，她自动认得（走真实 accept 路径）
    line = box.accept({"type": "动画表情", "local_id": 9, "sender_username": "wxid_demo",
                       "content": '<msg><emoji md5="abc123def" cdnurl="" /></msg>'}, "wxid_demo")
    print("[5] 同款表情 →", line)
    assert line == "[他发来一个表情包：狗头，反讽]", line

    # [6] 留空 = 取消
    code, r = call("/api/inbox/note", {"md5": "abc123def", "note": ""})
    print("[6] 取消注释:", code, r.get("ok"), "剩余", call("/api/inbox")[1]["known"])
    assert r["ok"] and call("/api/inbox")[1]["known"] == 0

    # [7] 缺参数
    code, r = call("/api/inbox/note", {"md5": "", "note": "x"})
    print("[7] 缺 md5:", code, r)
    assert code == 400

    # [8] 页面里有这张卡片
    code, html = call("/", raw=True)
    html = html.decode()
    for need in ("他发来的", "ibWall", "function refreshInbox", "/api/inbox/note"):
        assert need in html, "页面里缺少 " + need
    print("[8] 页面含收件箱卡片与脚本：OK")
finally:
    try:
        srv.shutdown()
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(TMP, ignore_errors=True)

print("\n全部通过")
