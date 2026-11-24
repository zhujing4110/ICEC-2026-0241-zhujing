# -*- coding: utf-8 -*-
"""验证「她的电脑」在控制台上的所有入口：状态、实时画面、截图、批准、越权。"""
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

import desk as D
import webui

PORT = 8804
TMP = Path(tempfile.mkdtemp(prefix="dkweb_"))
ok = 0


def check(name, cond, extra=""):
    global ok
    print(("  [OK] " if cond else "  [!!] ") + name + (f"   {extra}" if extra else ""))
    assert cond, name
    ok += 1


def call(path, payload=None, raw=False):
    url = f"http://127.0.0.1:{PORT}{path}"
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url) if payload is None else urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with op.open(req, timeout=15) as r:
            body = r.read()
            return r.status, (body if raw else json.loads(body.decode()))
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body.decode())
        except Exception:  # noqa: BLE001
            return e.code, body


class FakeDB:
    def get_nickname(self, u):
        return "测试者"


class FakeBridge:
    """只要控制台需要的那些字段，不连微信。"""

    def __init__(self, desk_obj):
        self.desk = desk_obj
        self.desk_requests = {}
        self.desk_results = {}
        self.desk_log = []
        self.desk_cfg = lambda: {"approve_timeout": 300}
        self.stickers = None
        self.inbox = None
        self.mem = None
        self.stickers = None
        self.paused = False

    # 复用真实 Bridge 的方法（逻辑就在里面，不能重复实现一份）
    desk_status = __import__("bridge").Bridge.desk_status
    approve_desk = __import__("bridge").Bridge.approve_desk
    _desk_shots = __import__("bridge").Bridge._desk_shots


print("[1] 起控制台")
dk = D.Desk(TMP / "base", {})
br = FakeBridge(dk)
srv = webui.start(br, "127.0.0.1", PORT)
check("服务起来了", srv is not None)
time.sleep(0.4)

print("[2] 状态接口")
code, st = call("/api/desk")
check("200", code == 200, str(code))
check("说能看", st["enabled"] is True)
check("暂无待批", st["pending_count"] == 0)
check("带允许目录", len(st["roots"]) > 0, str(st["roots"][:2]))

print("[3] 实时画面（现截一张）")
code, img = call("/api/desk/live", raw=True)
check("200", code == 200, str(code))
check("返回的是 jpeg", isinstance(img, bytes) and img[:2] == b"\xff\xd8", f"{len(img)//1024}KB")
check("体积合理（不会几百 KB 拖页面）", len(img) < 600_000, f"{len(img)//1024}KB")

print("[4] 她申请后，控制台能看到")
calls = [{"id": "c1", "tool": "look_screen", "args": {}, "preview": "看一眼你现在的屏幕（截图 + 读出上面的字）"}]
br.desk_requests["wxid_x"] = {"id": "r1", "calls": calls, "messages": [], "nick": "测试者",
                              "created": time.time(), "rounds": 1, "timeout": 300, "user_text": "看看"}
code, st = call("/api/desk")
check("待批 1 个", st["pending_count"] == 1)
check("带预览话术", "屏幕" in st["pending"][0]["calls"][0]["preview"])
check("带倒计时", st["pending"][0]["left"] > 200, f"{st['pending'][0]['left']}s")

print("[5] 点允许 → 执行 → 结果排队")
code, r = call("/api/desk/approve", {"user": "wxid_x", "id": "r1", "allow": True})
check("批准返回 ok", r.get("ok") is True, str(r))
check("工具执行了", len(dk.history) == 1, f"{len(dk.history)} 条")
check("流水里有截屏", dk.history[0]["tool"] == "look_screen")
check("生成了截图文件", bool(dk.history[0]["shot"]), str(dk.history[0]["shot"]))
check("结果等她接着说", "wxid_x" in br.desk_results)

print("[6] 截图能取回来")
shot = dk.history[0]["shot"]
code, img = call(f"/api/desk/shot/{shot}", raw=True)
check("200", code == 200)
check("是 png", img[:4] == b"\x89PNG", str(img[:4]))

print("[7] 越权取文件一律 404")
for bad in ["../../config.json", "..%2F..%2Fconfig.json", "C:/Windows/win.ini", "nope.png"]:
    code, _ = call(f"/api/desk/shot/{bad}", raw=True)
    check(f"{bad[:26]:<28} 404", code == 404, str(code))

print("[8] 参数不全 / 重复批准")
code, r = call("/api/desk/approve", {"user": "", "id": "r1", "allow": True})
check("缺参数 400", code == 400, str(code))
code, r = call("/api/desk/approve", {"user": "wxid_x", "id": "r1", "allow": True})
check("重复批准被拒", r.get("ok") is False, str(r.get("error")))

print("[9] 关掉之后状态怎么显示")
br.desk = None
code, st = call("/api/desk")
check("显示未启用", st["enabled"] is False)
code, _ = call("/api/desk/live", raw=True)
check("实时画面 503", code == 503, str(code))

srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n全部通过：{ok} 项")
