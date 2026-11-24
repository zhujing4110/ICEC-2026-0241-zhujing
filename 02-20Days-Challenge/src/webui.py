#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地 Web 控制台：一个页面看状态、改人设、翻日志、手动让她说句话。

只绑 127.0.0.1，不对外网开放。由 bridge.py 在启动时拉起来。
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

BASE = Path(__file__).resolve().parent
START_TS = time.time()
BRIDGE = None  # type: ignore[var-annotated]
PORT = 8765


def tail_log(n: int = 120) -> List[str]:
    p = BASE / "logs" / "bridge.log"
    if not p.exists():
        return ["（还没有日志）"]
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [ln.rstrip("\n") for ln in lines[-n:]]
    except OSError as exc:
        return [f"读日志失败: {exc}"]


def status_payload() -> Dict[str, Any]:
    br = BRIDGE
    now = time.time()
    out: Dict[str, Any] = {
        "running": bool(br and getattr(br, "db", None)),
        "uptime": int(now - START_TS),
        "account": getattr(br, "account", "") or "",
        "nickname": getattr(br, "account_nickname", "") or "",
        "paused": bool(getattr(br, "paused", False)),
        "today_proactive": 0,
        "sent_last_hour": 0,
        "target": "",
        "memory_count": 0,
        "pending": 0,
        "kb": {},
    }
    if not br:
        return out

    out["pending"] = len(getattr(br, "pending", {}) or {})

    today = time.strftime("%Y-%m-%d")
    for rec in (br.state.get("proactive") or {}).values():
        if isinstance(rec, dict) and rec.get("date") == today:
            out["today_proactive"] += int(rec.get("count") or 0)

    out["sent_last_hour"] = len([t for t in getattr(br, "send_log", []) if now - t < 3600])
    try:
        out["target"] = br._proactive_target() or ""
    except Exception:
        out["target"] = ""
    if br.mem is not None and out["target"]:
        out["memory_count"] = len(br.mem.load(out["target"]))
    if br.kb is not None:
        try:
            st = br.kb.stats()
            out["kb"] = {"files": st.get("files"), "chunks": st.get("chunks")}
        except Exception:
            out["kb"] = {}
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "xiaoyu-console/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        return  # 不把每个请求塞进 stderr

    # -- 基础响应
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json_out(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def html_out(self, text: str) -> None:
        self._send(200, text.encode("utf-8"), "text/html; charset=utf-8")

    def file_out(self, path: Path, ctype: str) -> None:
        try:
            self._send(200, path.read_bytes(), ctype)
        except OSError:
            self.json_out({"error": "read failed"}, 500)

    def read_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > 4_000_000:
            # 4MB 是给表情包上传留的（base64 会涨 1/3，前端还会先缩到 512px）
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self.html_out(INDEX_HTML)
            return
        if path == "/api/status":
            self.json_out(status_payload())
            return
        if path == "/api/logs":
            try:
                n = max(20, min(600, int(q.get("n", ["120"])[0])))
            except ValueError:
                n = 120
            self.json_out({"lines": tail_log(n)})
            return
        if path == "/api/persona":
            self.json_out({"text": read_persona()})
            return
        if path == "/api/memory":
            user = q.get("user", [""])[0] or status_payload().get("target", "")
            br = BRIDGE
            if not br or br.mem is None or not user:
                self.json_out({"user": user, "count": 0, "items": []})
                return
            st = br.mem.stats(user)
            self.json_out({"user": user, **st})
            return
        if path == "/api/learn":
            br = BRIDGE
            if not br:
                self.json_out({"enabled": False, "pending": [], "notes": [], "stats": {}})
                return
            try:
                self.json_out(br.learn_status())
            except Exception as exc:  # noqa: BLE001
                self.json_out({"enabled": False, "error": str(exc)}, 500)
            return
        if path == "/api/stickers":
            br = BRIDGE
            if not br:
                self.json_out({"enabled": False, "items": [], "stats": {}})
                return
            try:
                self.json_out(br.sticker_status())
            except Exception as exc:  # noqa: BLE001
                self.json_out({"enabled": False, "error": str(exc)}, 500)
            return
        if path.startswith("/stickers/"):
            br = BRIDGE
            name = unquote(path[len("/stickers/"):])
            p = br.stickers.path_of(name) if (br and br.stickers) else None
            if not p:
                self.json_out({"error": "not found"}, 404)
                return
            self.file_out(p, "image/png")
            return
        if path == "/api/desk":
            br = BRIDGE
            if not br:
                self.json_out({"enabled": False, "pending": [], "history": [], "shots": []})
                return
            try:
                self.json_out(br.desk_status())
            except Exception as exc:  # noqa: BLE001
                self.json_out({"enabled": False, "error": str(exc)}, 500)
            return
        if path == "/api/desk/live":
            # 现截一张，压成 jpeg —— 这张图是给"人"看的，她本人读的是 OCR 出来的字
            br = BRIDGE
            if not br or br.desk is None:
                self.json_out({"error": "看电脑未启用"}, 503)
                return
            try:
                import io

                from PIL import ImageGrab
                im = ImageGrab.grab(all_screens=True)
                w = 900
                im = im.resize((w, int(im.height * w / im.width)))
                buf = io.BytesIO()
                im.convert("RGB").save(buf, "JPEG", quality=55)
                self._send(200, buf.getvalue(), "image/jpeg")
            except Exception as exc:  # noqa: BLE001
                self.json_out({"error": str(exc)}, 500)
            return
        if path.startswith("/api/desk/shot/"):
            br = BRIDGE
            name = Path(unquote(path[len("/api/desk/shot/"):])).name   # 挡住 ../
            if not br or br.desk is None or not name:
                self.json_out({"error": "not found"}, 404)
                return
            p = br.desk.shot_dir / name
            if not p.is_file():
                self.json_out({"error": "not found"}, 404)
                return
            self.file_out(p, "image/png")
            return
        if path == "/api/inbox":
            br = BRIDGE
            if not br or br.inbox is None:
                self.json_out({"enabled": False, "count": 0, "items": []})
                return
            try:
                self.json_out({"enabled": True, **br.inbox.status()})
            except Exception as exc:  # noqa: BLE001
                self.json_out({"enabled": False, "error": str(exc)}, 500)
            return
        if path.startswith("/inbox/"):
            br = BRIDGE
            name = Path(unquote(path[len("/inbox/"):])).name   # 只认文件名，挡住 ../
            if not name or not br or br.inbox is None:
                self.json_out({"error": "not found"}, 404)
                return
            for sub in ("emojis", "images"):
                p = br.inbox.base / sub / name
                if p.is_file():
                    self.file_out(p, "image/png")
                    return
            self.json_out({"error": "not found"}, 404)
            return
        self.json_out({"error": "not found"}, 404)

    # -- POST
    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        body = self.read_body()
        br = BRIDGE

        if path == "/api/pause":
            if not br:
                self.json_out({"ok": False, "error": "bridge 未就绪"}, 503)
                return
            br.paused = bool(body.get("paused"))
            self.json_out({"ok": True, "paused": br.paused})
            return

        if path == "/api/persona":
            text = body.get("text")
            if not isinstance(text, str) or len(text) > 20000:
                self.json_out({"ok": False, "error": "内容为空或过长"}, 400)
                return
            try:
                write_persona(text)
            except OSError as exc:
                self.json_out({"ok": False, "error": str(exc)}, 500)
                return
            self.json_out({"ok": True, "bytes": len(text.encode("utf-8"))})
            return

        if path == "/api/proactive":
            if not br:
                self.json_out({"ok": False, "error": "bridge 未就绪"}, 503)
                return
            kind = body.get("kind") or "followup"
            if kind not in ("morning", "evening", "followup"):
                self.json_out({"ok": False, "error": "未知类型"}, 400)
                return
            try:
                text = br.maybe_proactive(force_kind=kind)
            except Exception as exc:  # noqa: BLE001
                self.json_out({"ok": False, "error": f"{exc}"}, 500)
                return
            self.json_out({"ok": bool(text), "text": text or "",
                           "error": "" if text else "生成或发送失败，看日志"})
            return

        if path == "/api/desk/approve":
            if not br or br.desk is None:
                self.json_out({"ok": False, "error": "看电脑未启用"}, 503)
                return
            user = (body.get("user") or "").strip()
            req_id = (body.get("id") or "").strip()
            allow = bool(body.get("allow"))
            if not user or not req_id:
                self.json_out({"ok": False, "error": "缺参数"}, 400)
                return
            try:
                self.json_out(br.approve_desk(user, req_id, allow))
            except Exception as exc:  # noqa: BLE001
                self.json_out({"ok": False, "error": str(exc)}, 500)
            return

        if path == "/api/learn":
            if not br:
                self.json_out({"ok": False, "error": "bridge 未就绪"}, 503)
                return
            topic = (body.get("topic") or "").strip()
            try:
                self.json_out(br.learn_now(topic))
            except Exception as exc:  # noqa: BLE001
                self.json_out({"ok": False, "error": f"{exc}"}, 500)
            return

        if path == "/api/learn/note":
            if not br:
                self.json_out({"ok": False, "error": "bridge 未就绪"}, 503)
                return
            from learn import delete_note

            name = (body.get("file") or "").strip()
            ok = bool(name) and delete_note(br.learned_dir(), name)
            self.json_out({"ok": ok, "error": "" if ok else "删不掉，看看文件名"})
            return

        if path == "/api/curiosity/drop":
            if not br or br.curio is None:
                self.json_out({"ok": False, "error": "自学未启用"}, 503)
                return
            tid = (body.get("id") or "").strip()
            self.json_out({"ok": bool(tid) and br.curio.remove(tid)})
            return

        if path == "/api/stickers/collect":
            if not br or br.stickers is None:
                self.json_out({"ok": False, "error": "表情包未启用"}, 503)
                return
            kw = (body.get("keyword") or "").strip()
            try:
                self.json_out(br.collect_now(kw))
            except Exception as exc:  # noqa: BLE001
                self.json_out({"ok": False, "error": f"{exc}"}, 500)
            return

        if path == "/api/stickers/delete":
            if not br or br.stickers is None:
                self.json_out({"ok": False, "error": "表情包未启用"}, 503)
                return
            name = (body.get("file") or "").strip()
            ok = bool(name) and br.stickers.delete(name)
            self.json_out({"ok": ok, "error": "" if ok else "删不掉，看看文件名"})
            return

        if path == "/api/stickers/retag":
            if not br or br.stickers is None:
                self.json_out({"ok": False, "error": "表情包未启用"}, 503)
                return
            name = (body.get("file") or "").strip()
            tags = body.get("tags")
            if isinstance(tags, str):
                tags = re.split(r"[,，、\s]+", tags)
            if not isinstance(tags, list):
                tags = []
            r = br.stickers.retag(name, tags)
            self.json_out(r, 200 if r.get("ok") else 400)
            return

        if path == "/api/stickers/upload":
            if not br or br.stickers is None:
                self.json_out({"ok": False, "error": "表情包未启用"}, 503)
                return
            data = body.get("data") or ""
            if not isinstance(data, str) or not data:
                self.json_out({"ok": False, "error": "没有收到图片"}, 400)
                return
            if "," in data:                       # data:image/png;base64,xxxx
                data = data.split(",", 1)[1]
            try:
                raw = base64.b64decode(data, validate=False)
            except Exception:                     # noqa: BLE001
                self.json_out({"ok": False, "error": "图片数据解不开"}, 400)
                return
            tags = body.get("tags")
            if isinstance(tags, str):
                tags = re.split(r"[,，、\s]+", tags)
            if not isinstance(tags, list):
                tags = []
            r = br.stickers.add_local_data(raw, tags)
            self.json_out(r, 200 if r.get("ok") else 400)
            return

        if path == "/api/inbox/note":
            if not br or br.inbox is None:
                self.json_out({"ok": False, "error": "收件箱未启用"}, 503)
                return
            md5 = str(body.get("md5") or "").strip()
            note = str(body.get("note") or "").strip()
            if not md5:
                self.json_out({"ok": False, "error": "缺少 md5"}, 400)
                return
            # 留空就是取消，别把空字符串当"认识"
            ok = br.inbox.note(md5, note) if note else br.inbox.forget_note(md5)
            self.json_out({"ok": ok, "error": "" if ok else "没这条记录"})
            return

        self.json_out({"error": "not found"}, 404)


def read_persona() -> str:
    p = BASE / "persona.md"
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_persona(text: str) -> None:
    p = BASE / "persona.md"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)


def start(bridge: Any, host: str = "127.0.0.1", port: int = 8765) -> Optional[ThreadingHTTPServer]:
    """在后台线程拉起控制台。端口被占就返回 None，不影响机器人本体。"""
    global BRIDGE, PORT
    BRIDGE = bridge
    PORT = port
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"[webui] 端口 {port} 启动失败：{exc}（机器人继续运行）")
        return None
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, name="webui", daemon=True).start()
    print(f"[webui] 控制台已启动 http://{host}:{port}/")
    return srv


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>小予 控制台</title>
<style>
  :root{
    --bg:#f5f6f8; --card:#fff; --line:#e3e6ea; --text:#1d2129;
    --mut:#6b7280; --accent:#2563eb; --ok:#16a34a; --warn:#dc2626;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
  header{display:flex;align-items:center;gap:12px;padding:14px 22px;background:var(--card);
         border-bottom:1px solid var(--line);position:sticky;top:0;z-index:9}
  header h1{font-size:16px;margin:0;font-weight:600}
  .dot{width:8px;height:8px;border-radius:50%;background:#cbd5e1;display:inline-block}
  .dot.on{background:var(--ok);box-shadow:0 0 0 3px rgba(22,163,74,.15)}
  .dot.off{background:var(--warn)}
  header .sp{flex:1}
  button{font:inherit;padding:6px 14px;border:1px solid var(--line);background:#fff;
         border-radius:7px;cursor:pointer;color:var(--text)}
  button:hover{border-color:#c7cdd6}
  button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
  button.primary:hover{background:#1d4ed8}
  button:disabled{opacity:.5;cursor:default}
  main{max-width:1180px;margin:18px auto 40px;padding:0 18px;
       display:grid;grid-template-columns:1fr 1fr;gap:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:16px 18px}
  .card.wide{grid-column:1/-1}
  .card h2{font-size:13px;margin:0 0 12px;font-weight:600;color:var(--mut);
           text-transform:none;letter-spacing:.02em}
  .card h2 small{font-weight:400;color:#9aa3af;margin-left:6px}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:12px}
  .stat .k{font-size:11px;color:var(--mut)}
  .stat .v{font-size:19px;font-weight:600;margin-top:2px}
  textarea{width:100%;height:330px;padding:11px;border:1px solid var(--line);border-radius:8px;
           font:13px/1.7 ui-monospace,Consolas,"Microsoft YaHei",monospace;resize:vertical;
           background:#fcfcfd;color:var(--text)}
  textarea:focus{outline:2px solid rgba(37,99,235,.25);border-color:var(--accent)}
  .row{display:flex;gap:9px;align-items:center;margin-top:10px;flex-wrap:wrap}
  .hint{color:var(--mut);font-size:12px}
  ul.mem{list-style:none;margin:0;padding:0;max-height:260px;overflow:auto;
         display:grid;grid-template-columns:1fr 1fr;gap:0 28px;align-content:start}
  ul.mem li{padding:7px 0;border-bottom:1px dashed var(--line);font-size:13px}
  ul.mem li:last-child{border-bottom:0}
  ul.mem li span{color:#9aa3af;font-size:11px;margin-left:6px}
  pre.log{margin:0;background:#fbfbfc;border:1px solid var(--line);border-radius:8px;
          padding:11px;height:280px;overflow:auto;font:12px/1.6 ui-monospace,Consolas,monospace;
          color:#374151;white-space:pre-wrap;word-break:break-all}
  .msg{font-size:12px}
  .msg.ok{color:var(--ok)} .msg.err{color:var(--warn)}
  .split{display:grid;grid-template-columns:1fr 1fr;gap:0 26px;margin-top:6px}
  ul.list{list-style:none;margin:4px 0 0;padding:0;max-height:232px;overflow:auto}
  ul.list li{display:flex;gap:8px;justify-content:space-between;align-items:center;
             padding:6px 0;border-bottom:1px dashed var(--line);font-size:13px}
  ul.list li:last-child{border-bottom:0}
  ul.list li i{color:#9aa3af;font-size:11px;font-style:normal;margin-left:5px}
  ul.list li button{padding:0 8px;font-size:11px;color:var(--mut)}
  input[type=text]{flex:1;min-width:190px;padding:7px 10px;border:1px solid var(--line);
                   border-radius:7px;font:inherit;color:var(--text);background:#fcfcfd}
  input[type=text]:focus{outline:2px solid rgba(37,99,235,.25);border-color:var(--accent)}
  .wall{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:10px;
        margin-top:12px;max-height:340px;overflow:auto;padding-right:4px}
  .st{position:relative;border:1px solid var(--line);border-radius:9px;overflow:hidden;
      background:#fbfbfc;aspect-ratio:1;display:flex;align-items:center;justify-content:center}
  .st img{width:100%;height:100%;object-fit:contain;display:block}
  .st b{position:absolute;left:0;right:0;bottom:0;background:rgba(255,255,255,.9);
        font:11px/1.5 inherit;font-weight:400;color:#6b7280;padding:1px 5px;
        white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .st button{position:absolute;top:3px;opacity:0;padding:0 6px;font-size:11px;
             line-height:16px;background:rgba(255,255,255,.92);border-radius:5px;
             border:1px solid var(--line);cursor:pointer}
  .st button.del{right:3px;color:#b91c1c}
  .st button.ed{left:3px;color:#2563eb}
  .st:hover button{opacity:1}
  .ib2{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;
       margin-top:12px;max-height:420px;overflow:auto;padding-right:4px}
  .ib2 .cell{border:1px solid var(--line);border-radius:9px;overflow:hidden;background:#fbfbfc}
  .ib2 .pic{aspect-ratio:1;display:flex;align-items:center;justify-content:center;background:#f3f4f6}
  .ib2 .pic img{width:100%;height:100%;object-fit:contain;display:block}
  .ib2 .meta{padding:4px 6px;font-size:11px;color:#6b7280;line-height:1.5}
  .ib2 .meta .t{color:#374151;display:block;white-space:nowrap;overflow:hidden;
                text-overflow:ellipsis}
  .ib2 .cell form{display:flex;gap:4px;padding:0 6px 6px}
  .ib2 .cell input{font-size:11px;padding:2px 5px;min-width:0;flex:1;
                   border:1px solid var(--line);border-radius:5px}
  .ib2 .cell button{font-size:11px;padding:1px 6px;border-radius:5px;
                    border:1px solid var(--line);background:#fff;cursor:pointer}
  .ib2 .cell .known{color:#059669}
  .dk-body{display:grid;grid-template-columns:1.35fr 1fr;gap:14px;margin-top:12px}
  .dk-shot{border:1px solid var(--line);border-radius:9px;overflow:hidden;background:#0b0f14}
  .dk-shot img{width:100%;display:block;min-height:110px;object-fit:contain}
  .dk-bar{display:flex;align-items:center;gap:10px;padding:6px 10px;background:#fbfbfc;
          border-top:1px solid var(--line);font-size:12px;color:var(--mut)}
  .dk-bar label{display:flex;align-items:center;gap:5px;cursor:pointer}
  .dk-sub{font-size:12px;color:var(--mut);margin-bottom:6px}
  .dk-log ul{list-style:none;margin:0;padding:0;max-height:286px;overflow:auto}
  .dk-log li{padding:5px 0;border-bottom:1px dashed var(--line);font-size:12.5px;line-height:1.5}
  .dk-log .t{color:var(--mut);font-variant-numeric:tabular-nums;margin-right:6px}
  .dk-log .bad{color:var(--warn)}
  .dk-log .good{color:var(--ok)}
  .dk-ask{display:none;border:1px solid #f0b429;background:#fffbeb;border-radius:9px;
          padding:10px 12px;margin-top:10px;box-shadow:0 1px 3px rgba(240,180,41,.16)}
  .dk-ask.on{display:block}
  .dk-ask .t{font-weight:600;margin-bottom:8px}
  .dk-ask .acts{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .dk-ask .acts .hint{margin-left:auto}
  @media(max-width:820px){main{grid-template-columns:1fr}
    .dk-body{grid-template-columns:1fr}
    header{flex-wrap:wrap}
    ul.mem{grid-template-columns:1fr}
    .split{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <h1>小予 控制台</h1>
  <span class="dot" id="dot"></span>
  <span class="hint" id="stateText">连接中…</span>
  <span class="sp"></span>
  <button id="pauseBtn">暂停</button>
</header>

<main>
  <section class="card">
    <h2>运行状态</h2>
    <div class="grid" id="stats"></div>
  </section>

  <section class="card">
    <h2>主动消息 <small>手动触发，会真的发出去</small></h2>
    <p class="hint">自动规则之外，想让她现在就说一句：</p>
    <div class="row">
      <button data-kind="morning">早安</button>
      <button data-kind="evening">晚安</button>
      <button data-kind="followup">接上一句</button>
    </div>
    <div class="row"><span class="msg" id="pmsg"></span></div>
    <p class="hint" id="targetHint"></p>
  </section>

  <section class="card wide">
    <h2>他发来的 <small id="ibHint">图片、表情包、语音、文件 —— 她收得到，但看不到画面内容</small></h2>
    <p class="hint">她认得出的表情包会带上情绪（比如「无语」）。认不出的，在图上写一句「这是狗头，反讽」，以后同款她就知道是什么意思。</p>
    <div class="ib2" id="ibWall"><p class="hint">加载中…</p></div>
  </section>

  <section class="card wide">
    <h2>她的电脑 <small id="dkHint">只读 · 每次调用都要你点允许</small></h2>
    <div class="dk-ask" id="dkAsk">
      <div class="t" id="dkAskText"></div>
      <div class="acts">
        <button class="primary" id="dkYes">允许</button>
        <button id="dkNo">拒绝</button>
        <span class="hint" id="dkLeft"></span>
      </div>
    </div>
    <div class="dk-body">
      <div class="dk-shot">
        <img id="dkLive" alt="她的屏幕画面">
        <div class="dk-bar">
          <label><input type="checkbox" id="dkAuto" checked> 实时画面</label>
          <span id="dkShotHint">每 10 秒刷新</span>
        </div>
      </div>
      <div class="dk-log">
        <div class="dk-sub">她做了什么</div>
        <ul id="dkHist"><li class="hint">还没动过</li></ul>
      </div>
    </div>
  </section>

  <section class="card wide">
    <h2>人设 <small>persona.md · 保存后下一条消息生效，不用重启</small></h2>
    <textarea id="persona" spellcheck="false"></textarea>
    <div class="row">
      <button class="primary" id="savePersona">保存人设</button>
      <button id="reloadPersona">重新读取</button>
      <span class="msg" id="pmsg2"></span>
    </div>
  </section>

  <section class="card wide">
    <h2>长期记忆 <small id="memUser"></small></h2>
    <ul class="mem" id="mem"><li class="hint">加载中…</li></ul>
  </section>

  <section class="card wide">
    <h2>好奇心与自学 <small>她想知道的问题，空闲时自己上网查，写成笔记</small></h2>
    <div class="row">
      <input type="text" id="learnTopic" placeholder="给个话题，或留空让她从队列里挑">
      <button class="primary" id="learnNow">现在学一个</button>
      <span class="msg" id="lmsg"></span>
    </div>
    <div class="split">
      <div>
        <div class="hint" id="queueHint">待学</div>
        <ul class="list" id="queue"><li class="hint">加载中…</li></ul>
      </div>
      <div>
        <div class="hint" id="noteHint">已学</div>
        <ul class="list" id="notes"><li class="hint">加载中…</li></ul>
      </div>
    </div>
    <p class="hint">笔记存在 knowledge/learned/，会自动进知识库，聊到相关话题时用得上；她不会主动炫耀学过什么。</p>
  </section>

  <section class="card wide">
    <h2>表情包 <small id="stHint">她自己上网搜图攒的，聊天时按情绪发</small></h2>
    <div class="row">
      <input type="text" id="stKeyword" placeholder="收一个词的表情包，比如 无语 / 猫；留空按顺序自动挑">
      <button class="primary" id="stCollect">去收表情包</button>
      <button id="stPickFile">传本地图</button>
      <input type="file" id="stFile" accept="image/*" multiple hidden>
      <span class="msg" id="stMsg"></span>
    </div>
    <p class="hint" style="margin-top:8px">鼠标移到图上可以「改」标签或「删」。标签是挑图的唯一依据，写错了这张就发不出去。</p>
    <div class="wall" id="stWall"><p class="hint">加载中…</p></div>
  </section>

  <section class="card wide">
    <h2>日志 <small>最近 120 行，3 秒刷新</small></h2>
    <pre class="log" id="log"></pre>
  </section>
</main>

<script>
const $ = (id) => document.getElementById(id);

function fmtTime(sec){
  const h = Math.floor(sec/3600), m = Math.floor(sec%3600/60), s = sec%60;
  if (h) return h + ' 小时 ' + m + ' 分';
  if (m) return m + ' 分 ' + s + ' 秒';
  return s + ' 秒';
}

async function getJSON(url){
  const r = await fetch(url, {cache:'no-store'});
  return r.json();
}

async function postJSON(url, data){
  const r = await fetch(url, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(data || {})
  });
  return r.json();
}

async function refreshStatus(){
  try{
    const s = await getJSON('/api/status');
    const dot = $('dot');
    dot.className = 'dot ' + (s.paused ? 'off' : (s.running ? 'on' : ''));
    $('stateText').textContent = s.paused ? '已暂停' : (s.running ? '运行中' : '未连接微信');
    $('pauseBtn').textContent = s.paused ? '恢复' : '暂停';

    const kb = s.kb && s.kb.chunks ? (s.kb.files + ' 文件 / ' + s.kb.chunks + ' 片段') : '未启用';
    $('stats').innerHTML = [
      ['账号', s.nickname || s.account || '—'],
      ['已运行', fmtTime(s.uptime)],
      ['今日主动', s.today_proactive + ' 条'],
      ['近1小时发送', s.sent_last_hour + ' 条'],
      ['长期记忆', s.memory_count + ' 条'],
      ['等你说完', (s.pending || 0) + ' 个'],
      ['知识库', kb],
    ].map(([k,v]) => '<div class="stat"><div class="k">'+k+'</div><div class="v">'+v+'</div></div>').join('');
    $('targetHint').textContent = s.target ? ('当前对象：' + s.target) : '';
  }catch(e){
    $('stateText').textContent = '控制台连不上';
    $('dot').className = 'dot off';
  }
}

async function refreshMemory(){
  try{
    const m = await getJSON('/api/memory');
    $('memUser').textContent = m.user ? '(' + m.count + ' 条)' : '';
    const ul = $('mem');
    if (!m.items || !m.items.length){
      ul.innerHTML = '<li class="hint">还没有记忆。聊几句就会自己攒起来。</li>';
      return;
    }
    ul.innerHTML = m.items.slice().reverse().map(it => {
      const d = it.ts ? new Date(it.ts*1000) : null;
      const t = d ? ('<span>' + (d.getMonth()+1) + '/' + d.getDate() + '</span>') : '';
      return '<li>' + escapeHTML(it.text) + t + '</li>';
    }).join('');
  }catch(e){}
}

async function refreshLogs(){
  try{
    const d = await getJSON('/api/logs?n=120');
    const el = $('log');
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
    el.textContent = (d.lines || []).join('\n');
    if (atBottom) el.scrollTop = el.scrollHeight;
  }catch(e){}
}

function escapeHTML(s){
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

$('pauseBtn').onclick = async () => {
  const paused = $('pauseBtn').textContent.trim() === '暂停';
  const r = await postJSON('/api/pause', {paused});
  if (r.ok) refreshStatus();
};

$('savePersona').onclick = async () => {
  const r = await postJSON('/api/persona', {text: $('persona').value});
  const m = $('pmsg2');
  if (r.ok){ m.className='msg ok'; m.textContent='已保存，下一条消息生效'; }
  else { m.className='msg err'; m.textContent='保存失败：' + (r.error||''); }
  setTimeout(()=>{ m.textContent=''; }, 4000);
};

$('reloadPersona').onclick = loadPersona;

async function loadPersona(){
  const d = await getJSON('/api/persona');
  $('persona').value = d.text || '';
}

document.querySelectorAll('[data-kind]').forEach(btn => {
  btn.onclick = async () => {
    const kind = btn.dataset.kind;
    if (!confirm('确认现在就让她发一条？会真的发到微信。')) return;
    btn.disabled = true;
    const m = $('pmsg');
    m.className = 'msg'; m.textContent = '生成中…';
    const r = await postJSON('/api/proactive', {kind});
    if (r.ok){ m.className='msg ok'; m.textContent='已发出：' + r.text; }
    else { m.className='msg err'; m.textContent='失败：' + (r.error||''); }
    btn.disabled = false;
    refreshStatus(); refreshLogs();
    setTimeout(()=>{ m.textContent=''; }, 8000);
  };
});

async function refreshLearn(){
  try{
    const d = await getJSON('/api/learn');
    const q = $('queue'), n = $('notes');
    if (!d.enabled){
      q.innerHTML = '<li class="hint">自学未启用</li>';
      n.innerHTML = '<li class="hint">—</li>';
      return;
    }
    const st = d.stats || {};
    $('queueHint').textContent = '待学 ' + (st.pending||0) + ' 条' +
      (st.skipped ? ' · 放弃 ' + st.skipped : '') + (d.learning ? ' · 正在学…' : '');
    $('noteHint').textContent = '已学 ' + (st.done||0) + ' 篇 · 今日 ' + (st.today||0) + ' 条';

    const pend = d.pending || [];
    q.innerHTML = pend.length ? pend.map(x =>
      '<li><span>' + escapeHTML(x.topic) +
      '<i>' + (x.source === 'chat' ? '聊天里' : x.source === 'manual' ? '手动' : '自己想的') + '</i></span>' +
      '<button data-drop="' + escapeHTML(x.id) + '">删</button></li>').join('')
      : '<li class="hint">队列是空的，她想到什么会自己加。</li>';

    const notes = d.notes || [];
    n.innerHTML = notes.length ? notes.map(x =>
      '<li><span>' + escapeHTML(x.topic) + '<i>' + escapeHTML(x.at) + '</i></span>' +
      '<button data-note="' + escapeHTML(x.file) + '">删</button></li>').join('')
      : '<li class="hint">还没学到东西。</li>';
  }catch(e){}
}

$('learnNow').onclick = async () => {
  const topic = $('learnTopic').value.trim();
  const m = $('lmsg');
  m.className = 'msg'; m.textContent = '开始查，大概十秒…';
  const r = await postJSON('/api/learn', {topic});
  if (r.ok){
    m.className = 'msg ok'; m.textContent = '正在学：' + r.topic;
    $('learnTopic').value = '';
  } else {
    m.className = 'msg err'; m.textContent = '没学成：' + (r.error||'');
  }
  refreshLearn();
  setTimeout(refreshLearn, 12000);
  setTimeout(()=>{ m.textContent=''; }, 9000);
};

document.addEventListener('click', async (e) => {
  const drop = e.target.closest('[data-drop]');
  if (drop){
    await postJSON('/api/curiosity/drop', {id: drop.dataset.drop});
    refreshLearn(); return;
  }
  const note = e.target.closest('[data-note]');
  if (note){
    if (!confirm('删掉这篇笔记？知识库里对应的内容也会一起失效。')) return;
    await postJSON('/api/learn/note', {file: note.dataset.note});
    refreshLearn();
  }
});

async function refreshInbox(){
  try{
    const d = await getJSON('/api/inbox');
    const wall = $('ibWall');
    if (!d.enabled){
      wall.innerHTML = '<p class="hint">收件箱未启用（config.json 里 inbox.enabled）</p>';
      return;
    }
    const kinds = Object.entries(d.kinds || {}).map(([k,v]) => k + ' ' + v).join(' · ');
    $('ibHint').textContent = '最近 ' + (d.count||0) + ' 条' +
      (kinds ? ' · ' + kinds : '') + ' · 已认识 ' + (d.known||0) + ' 张表情';

    const items = d.items || [];
    wall.innerHTML = items.length ? items.map((x, i) => {
      const name = (x.file || '').split(/[\\/]/).pop();
      const pic = name
        ? '<div class="pic"><img src="/inbox/' + encodeURIComponent(name) + '" alt=""></div>'
        : '<div class="pic" style="font-size:12px;color:#9ca3af">' +
          escapeHTML(x.type || '') + '</div>';
      const label = x.label
        ? '<span class="known">已认识：' + escapeHTML(x.label) + '</span>'
        : '认不出这张';
      const ph = x.label ? '改一句说明，留空就取消' : '这是啥？写一句她就懂了';
      return '<div class="cell">' + pic +
        '<div class="meta"><span class="t">' + escapeHTML(x.line || '') + '</span>' +
        escapeHTML((x.at||'').slice(5)) + ' · ' + label + '</div>' +
        (x.md5 ? '<form data-note="' + escapeHTML(x.md5) + '">' +
          '<input type="text" placeholder="' + ph + '" value="' +
          escapeHTML(x.label || '') + '"><button>记住</button></form>' : '') +
        '</div>';
    }).join('') : '<p class="hint">还没有。你在微信里发张图或者发个表情包试试。</p>';
  }catch(e){}
}

document.addEventListener('submit', async (e) => {
  const f = e.target.closest('[data-note]');
  if (!f) return;
  e.preventDefault();
  const note = f.querySelector('input').value.trim();
  await postJSON('/api/inbox/note', {md5: f.dataset.note, note});
  refreshInbox();
});

// ---------------------------------------------------------------- 她的电脑
let dkPending = null;      // 当前要批的请求
let dkLiveTimer = null;

function dkFmtLeft(sec){
  if (sec <= 0) return '已超时';
  const m = Math.floor(sec / 60), s = sec % 60;
  return '还剩 ' + m + ':' + String(s).padStart(2, '0');
}

async function refreshDesk(){
  const d = await getJSON('/api/desk');
  if (!d || !d.enabled){
    $('dkHint').textContent = '没启用（config.json 里 desk.enabled）';
    $('dkAsk').className = 'dk-ask';
    return;
  }
  $('dkHint').textContent = '只读 · 每次调用都要你点允许';

  // --- 待批准
  const first = (d.pending || [])[0] || null;
  dkPending = first;
  if (first){
    const what = (first.calls || []).map(c => c.preview).join('；');
    $('dkAskText').textContent = first.nick + ' 想：' + what;
    $('dkLeft').textContent = dkFmtLeft(first.left);
    $('dkAsk').className = 'dk-ask on';
  } else {
    $('dkAsk').className = 'dk-ask';
  }

  // --- 流水
  const ul = $('dkHist');
  const hist = d.history || [];
  if (!hist.length){
    ul.innerHTML = '<li class="hint">还没动过。你在微信里让她看东西，这里会一条条记下来。</li>';
  } else {
    ul.innerHTML = hist.map(h =>
      '<li><span class="t">' + escapeHTML(h.at || '') + '</span>' +
      '<span class="' + (h.ok ? 'good' : 'bad') + '">' + escapeHTML(h.detail || h.tool || '') +
      '</span>' + (h.ms ? '<span class="t"> · ' + h.ms + 'ms</span>' : '') + '</li>'
    ).join('');
  }
}

async function dkApprove(allow){
  if (!dkPending) return;
  const btn = allow ? $('dkYes') : $('dkNo');
  btn.disabled = true;
  const r = await postJSON('/api/desk/approve',
                           {user: dkPending.user, id: dkPending.id, allow: allow});
  btn.disabled = false;
  $('dkAsk').className = 'dk-ask';
  if (!r || !r.ok) alert((r && r.error) || '操作失败');
  dkPending = null;
  refreshDesk();
  setTimeout(refreshDesk, 2500);
}

$('dkYes').onclick = () => dkApprove(true);
$('dkNo').onclick = () => dkApprove(false);

function dkTickLive(){
  if (!$('dkAuto').checked) return;
  if (document.hidden) return;           // 页面看不见就别一直截屏
  $('dkLive').src = '/api/desk/live?t=' + Date.now();
}

async function refreshStickers(){
  try{
    const d = await getJSON('/api/stickers');
    const wall = $('stWall');
    if (!d.enabled){
      wall.innerHTML = '<p class="hint">表情包未启用（config.json 里 stickers.enabled）</p>';
      return;
    }
    const st = d.stats || {};
    const tags = Object.entries(st.tags || {}).slice(0, 8)
      .map(([k,v]) => k + ' ' + v).join(' · ');
    $('stHint').textContent = (st.count||0) + ' 张 · ' + (st.size_mb||0) + ' MB' +
      (tags ? ' · ' + tags : '') + (d.collecting ? ' · 正在收…' : '');

    const items = d.items || [];
    wall.innerHTML = items.length ? items.map(x =>
      '<div class="st"><img src="/stickers/' + encodeURIComponent(x.file) + '" alt="">' +
      '<b>' + escapeHTML((x.tags||[]).join(' ')) + (x.uses ? ' ·' + x.uses : '') + '</b>' +
      '<button class="ed" data-rt="' + escapeHTML(x.file) + '" data-tags="' +
        escapeHTML((x.tags||[]).join(',')) + '">改</button>' +
      '<button class="del" data-st="' + escapeHTML(x.file) + '">删</button></div>').join('')
      : '<p class="hint">还没有表情包。点上面「去收表情包」，她会自己上网攒。</p>';
  }catch(e){}
}

$('stCollect').onclick = async () => {
  const kw = $('stKeyword').value.trim();
  const m = $('stMsg');
  m.className = 'msg'; m.textContent = '去搜了，十几秒…';
  const r = await postJSON('/api/stickers/collect', {keyword: kw});
  if (r.ok){
    m.className = 'msg ok'; m.textContent = '正在收：' + r.keyword;
    $('stKeyword').value = '';
  } else {
    m.className = 'msg err'; m.textContent = '没收成：' + (r.error||'');
  }
  setTimeout(refreshStickers, 8000);
  setTimeout(refreshStickers, 16000);
  setTimeout(()=>{ m.textContent=''; }, 12000);
};

document.addEventListener('click', async (e) => {
  const ed = e.target.closest('[data-rt]');
  if (ed){
    const v = prompt('改标签（多个用逗号隔开，标签是挑图的唯一依据）', ed.dataset.tags || '');
    if (v === null) return;
    const r = await postJSON('/api/stickers/retag', {file: ed.dataset.rt, tags: v});
    if (!r.ok) alert(r.reason || r.error || '改不动');
    refreshStickers();
    return;
  }
  const del = e.target.closest('[data-st]');
  if (!del) return;
  if (!confirm('删掉这张表情包？')) return;
  await postJSON('/api/stickers/delete', {file: del.dataset.st});
  refreshStickers();
});

$('stPickFile').onclick = () => $('stFile').click();

// 先在浏览器里缩到 512px 再传：一张手机截图十几 MB，原样塞过来既慢又会被请求体上限截断
async function fileToDataURL(f, max){
  try{
    const img = await new Promise((res, rej) => {
      const i = new Image();
      i.onload = () => res(i);
      i.onerror = rej;
      i.src = URL.createObjectURL(f);
    });
    const w = img.width || max, h = img.height || max;
    const s = Math.min(1, max / Math.max(w, h));
    const c = document.createElement('canvas');
    c.width = Math.max(1, Math.round(w * s));
    c.height = Math.max(1, Math.round(h * s));
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    return c.toDataURL('image/png');
  }catch(err){
    return await new Promise(res => {
      const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(f);
    });
  }
}

$('stFile').onchange = async (e) => {
  const files = Array.from(e.target.files || []);
  e.target.value = '';
  if (!files.length) return;
  const m = $('stMsg');
  const tags = prompt('给这 ' + files.length + ' 张图打个标签（多个用逗号隔开）', '其他');
  if (tags === null) return;
  m.className = 'msg'; m.textContent = '导入中…';
  let ok = 0; const bad = [];
  for (const f of files){
    const data = await fileToDataURL(f, 512);
    const r = await postJSON('/api/stickers/upload', {data, tags});
    if (r.ok) ok++; else bad.push(f.name + '：' + (r.reason || r.error || '失败'));
  }
  m.className = bad.length ? 'msg err' : 'msg ok';
  m.textContent = '导入 ' + ok + ' 张' + (bad.length ? '，' + bad.length + ' 张没成（' + bad[0] + '）' : '');
  refreshStickers();
  setTimeout(()=>{ m.textContent=''; }, 8000);
};

loadPersona();
refreshStatus(); refreshMemory(); refreshLogs(); refreshLearn(); refreshStickers(); refreshInbox();
refreshDesk(); dkTickLive();
setInterval(refreshStatus, 3000);
setInterval(refreshLogs, 3000);
setInterval(refreshMemory, 15000);
setInterval(refreshLearn, 10000);
setInterval(refreshStickers, 20000);
setInterval(refreshDesk, 3000);
setInterval(dkTickLive, 10000);
</script>
</body>
</html>
"""
