"""让她能"看"这台电脑：截屏读字、列目录、读文件、找文件、看系统状态。

设计前提（很重要，别自我欺骗）：
- 她背后的模型是纯文本的，**没有视觉能力**。所以"看屏幕"对她来说不是看图，
  而是截屏后交给 Windows 自带的 OCR 引擎（Windows.Media.Ocr，中文识别可用）
  变成文字。画面好不好看对她没意义，字读得准才有意义。
- 截图同时存一份给人在控制台看，这样你能知道"她眼里的屏幕"是什么样。

安全取向：
- **只读**。这个模块没有任何写文件、删文件、执行命令的能力，从设计上就不可能
  改坏东西（不是靠开关，是根本没有那条代码）。
- 路径先过沙箱：只允许桌面/文档/下载/图片 + 本项目目录；系统目录、AppData、
  .ssh、浏览器数据一律拒绝。
- 读出来的文本会把疑似密钥打码（本项目 config.json 里就有 api_key）。
- 每次调用都要人在控制台点"允许"才执行，这一步在 bridge 里做，本模块只管执行。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 工具定义

TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "look_screen",
            "description": "看你现在的屏幕。会把画面上的文字读出来（你说不出图里画了什么，但能看到文字）。"
                           "想知道对方在做什么、报错是什么、打开了什么网页时用。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "看一个文件夹里有什么。路径可以写 桌面 / 文档 / 下载 / 图片。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件夹路径，比如 桌面 或 D:/作品"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读一个文本文件的内容（txt/md/py/json/csv 等）。读不了图片和二进制。",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "按文件名找文件，不知道东西放在哪时用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "文件名里的关键词，比如 作品集"},
                    "root": {"type": "string", "description": "从哪个目录开始找，不填就找桌面/文档/下载"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sys_info",
            "description": "看电脑基本情况：现在几点、前台是哪个窗口、内存占用、电池。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

# 路径别名，模型写中文比写全路径可靠
PATH_ALIASES = {
    "桌面": "Desktop", "desktop": "Desktop",
    "文档": "Documents", "documents": "Documents",
    "下载": "Downloads", "downloads": "Downloads",
    "图片": "Pictures", "pictures": "Pictures",
}

# 一律不许进的目录名（小写比较）
DENY_DIR_NAMES = {
    "appdata", "windows", "system32", "syswow64", "program files",
    "program files (x86)", "programdata", "$recycle.bin", "system volume information",
    "node_modules", ".git", ".ssh", ".gnupg", ".aws", ".config",
    ".venv", "venv", "__pycache__", "site-packages", ".idea", ".vscode",
    "cookies", "logins", "login data", "web data", "local storage",
    "indexeddb", "cache", "cachestorage", "gpucache", "blob_storage",
}

# 一律不许读的文件名/后缀
DENY_FILE_RE = re.compile(
    r"(^\.env|\.env$|id_rsa|id_ed25519|\.pem$|\.pfx$|\.p12$|\.kdbx$|"
    r"^login data$|^cookies$|^web data$|password|passwd|secret|credential|"
    r"\.sqlite$|\.db$|\.dat$)",
    re.I,
)

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".py", ".json", ".csv", ".tsv", ".log", ".ini",
    ".cfg", ".conf", ".yml", ".yaml", ".toml", ".html", ".htm", ".css", ".js",
    ".ts", ".bat", ".ps1", ".sh", ".java", ".c", ".cpp", ".h", ".xml", ".sql",
}

# 密钥打码：本项目的 config.json 里就有 api_key，绝不能顺着这条路漏出去
SECRET_PATTERNS = [
    (re.compile(r'(sk-[A-Za-z0-9_\-]{12,})'), r'sk-***'),
    (re.compile(r'("(?:api_key|apikey|token|secret|password|authorization)"\s*:\s*")([^"]{6,})(")', re.I), r'\1***\3'),
    (re.compile(r'((?:api[_-]?key|token|secret|password)\s*[=:]\s*)([^\s"\']{8,})', re.I), r'\1***'),
    (re.compile(r'(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]+?(-----END [A-Z ]*PRIVATE KEY-----)'), r'\1 [已隐藏] \2'),
]

MAX_READ_BYTES = 96 * 1024
MAX_SHOT_KEEP = 24


def mask_secrets(text: str) -> str:
    for pat, rep in SECRET_PATTERNS:
        text = pat.sub(rep, text)
    return text


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"


@dataclass
class DeskResult:
    ok: bool
    text: str = ""                 # 给模型看的
    shot: str = ""                 # 给人看的截图文件名
    detail: str = ""               # 流水里的一句话
    meta: Dict[str, Any] = field(default_factory=dict)

    def as_tool_message(self) -> str:
        return self.text if self.ok else f"失败：{self.text}"


class Desk:
    """只读的"看电脑"能力。execute() 只在人工批准之后被调用。"""

    def __init__(self, base: Path, cfg: Optional[Dict[str, Any]] = None):
        self.base = Path(base)
        self.cfg = cfg or {}
        self.shot_dir = self.base / "desk_shots"
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.home = Path(os.environ.get("USERPROFILE") or Path.home())
        self.allow_roots = self._build_roots()
        # 操作流水，给控制台看
        self.history: List[Dict[str, Any]] = []

    # -- 沙箱 --------------------------------------------------------
    def _build_roots(self) -> List[Path]:
        roots = []
        for name in ("Desktop", "Documents", "Downloads", "Pictures"):
            p = self.home / name
            if p.exists():
                roots.append(p.resolve())
        roots.append(self.base.resolve())
        # 她自己的项目工作区（wxbot 的上一级）。笔记、作品集、交上去的东西多半在这一层，
        # 不含这一层的话"我那个文档放哪了"永远找不到。
        if self.base.parent != self.base and self.base.parent.exists():
            roots.append(self.base.parent.resolve())
        for extra in (self.cfg.get("extra_roots") or []):
            p = Path(str(extra)).expanduser()
            if p.exists() and p.resolve() not in roots:
                roots.append(p.resolve())
        return roots

    def resolve(self, raw: str) -> Tuple[Optional[Path], str]:
        """把模型给的路径解析成绝对路径，并判断是否允许。返回 (路径, 拒绝原因)。"""
        s = (raw or "").strip().strip('"').strip("'")
        if not s:
            return None, "路径是空的"
        # 别名
        first = s.split("/")[0].split("\\")[0]
        low = first.lower()
        if low in PATH_ALIASES:
            rest = s[len(first):].lstrip("/\\")
            s = str(self.home / PATH_ALIASES[low] / rest)
        p = Path(os.path.expandvars(s)).expanduser()
        if not p.is_absolute():
            p = (self.base / p)
        try:
            p = p.resolve()
        except OSError as exc:
            return None, f"路径不合法：{exc}"

        # 先看是否落在允许根里。命中就以「相对位置」判禁令，
        # 否则如果允许根本身就在 AppData 下（比如临时目录），会被全局禁令自己否掉。
        for root in self.allow_roots:
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            for part in rel.parts:
                if part.lower() in DENY_DIR_NAMES or part.startswith("."):
                    return None, f"「{part}」这类目录我不进去"
            return p, ""

        # 不在任何允许根里：给出具体原因，别只说"不行"
        for part in p.parts:
            if part.lower() in DENY_DIR_NAMES:
                return None, f"「{part}」这类目录我不进去"
        allowed = "、".join(r.name for r in self.allow_roots)
        return None, f"这个位置不在允许范围（只能看 {allowed}）"

    # -- 工具 --------------------------------------------------------
    def execute(self, tool: str, args: Dict[str, Any]) -> DeskResult:
        tool = (tool or "").strip()
        args = args or {}
        t0 = time.time()
        try:
            if tool == "look_screen":
                r = self._look_screen()
            elif tool == "list_dir":
                r = self._list_dir(str(args.get("path") or ""))
            elif tool == "read_file":
                r = self._read_file(str(args.get("path") or ""))
            elif tool == "find_files":
                r = self._find_files(str(args.get("keyword") or ""), str(args.get("root") or ""))
            elif tool == "sys_info":
                r = self._sys_info()
            else:
                r = DeskResult(False, f"没有这个工具：{tool}")
        except Exception as exc:  # 工具出问题不能把机器人带崩
            r = DeskResult(False, f"{type(exc).__name__}: {exc}")
        r.meta["ms"] = int((time.time() - t0) * 1000)
        self.history.append({
            "at": time.strftime("%H:%M:%S"),
            "tool": tool,
            "args": args,
            "ok": r.ok,
            "detail": r.detail,
            "shot": r.shot,
            "ms": r.meta["ms"],
        })
        self.history = self.history[-120:]
        return r

    def preview(self, tool: str, args: Dict[str, Any]) -> str:
        """给人在批准前看的一句话。"""
        a = args or {}
        if tool == "look_screen":
            return "看一眼你现在的屏幕（截图 + 读出上面的字）"
        if tool == "list_dir":
            return f"看看「{a.get('path','')}」里有什么"
        if tool == "read_file":
            return f"读一下「{a.get('path','')}」"
        if tool == "find_files":
            root = a.get("root") or "桌面/文档/下载"
            return f"在「{root}」里找名字带「{a.get('keyword','')}」的文件"
        if tool == "sys_info":
            return "看一下电脑状态（时间、前台窗口、内存）"
        return f"要用 {tool}"

    # -- 具体实现 ----------------------------------------------------
    def _look_screen(self) -> DeskResult:
        from PIL import ImageGrab

        im = ImageGrab.grab(all_screens=True)
        fg = _foreground_title()
        lines, err = _ocr_lines(im)

        name = f"shot_{time.strftime('%H%M%S')}.png"
        path = self.shot_dir / name
        # 存小一号的图就够人看，省磁盘
        thumb = im.resize((min(1280, im.width), int(im.height * min(1280, im.width) / im.width)))
        thumb.save(path, "PNG")
        self._trim_shots()

        if err:
            return DeskResult(True, f"屏幕是 {im.width}x{im.height}，前台窗口是「{fg}」。"
                                    f"但这次没读出文字（{err}）。", shot=name,
                              detail=f"截屏 {im.width}x{im.height}，OCR 没出字")

        body = _clean_ocr(lines)
        if not body:
            text = f"屏幕是 {im.width}x{im.height}，前台窗口是「{fg}」。上面没识别到什么文字。"
        else:
            text = (f"屏幕 {im.width}x{im.height}，前台窗口是「{fg}」。"
                    f"屏幕上的文字（从上到下）：\n{body}")
        return DeskResult(True, text, shot=name,
                          detail=f"截屏并读出 {len(body.splitlines())} 行字")

    def _list_dir(self, raw: str) -> DeskResult:
        p, why = self.resolve(raw)
        if not p:
            return DeskResult(False, f"看不了「{raw}」：{why}")
        if not p.exists():
            return DeskResult(False, f"「{raw}」不存在")
        if not p.is_dir():
            return DeskResult(False, f"「{raw}」不是文件夹")

        dirs, files = [], []
        try:
            for entry in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                if entry.name.startswith(".") or entry.name.lower() in DENY_DIR_NAMES:
                    continue
                try:
                    if entry.is_dir():
                        dirs.append(f"[夹] {entry.name}")
                    else:
                        st = entry.stat()
                        files.append(f"{entry.name}  {human_size(st.st_size)}  "
                                     f"{time.strftime('%m-%d %H:%M', time.localtime(st.st_mtime))}")
                except OSError:
                    continue
        except PermissionError:
            return DeskResult(False, f"「{raw}」没权限读")

        if not dirs and not files:
            return DeskResult(True, f"「{p.name}」是空的", detail=f"{p.name} 空文件夹")

        body = "\n".join((dirs + files)[:60])
        more = len(dirs) + len(files) - 60
        if more > 0:
            body += f"\n……还有 {more} 项"
        return DeskResult(True, f"「{p}」里有 {len(dirs)} 个文件夹、{len(files)} 个文件：\n{body}",
                          detail=f"{p.name}：{len(dirs)}夹 {len(files)}文件")

    def _read_file(self, raw: str) -> DeskResult:
        p, why = self.resolve(raw)
        if not p:
            return DeskResult(False, f"看不了「{raw}」：{why}")
        if not p.exists():
            return DeskResult(False, f"「{raw}」不存在")
        if not p.is_file():
            return DeskResult(False, f"「{raw}」不是文件")
        if DENY_FILE_RE.search(p.name):
            return DeskResult(False, f"「{p.name}」这类文件我不读")
        if p.suffix.lower() not in TEXT_EXTS:
            return DeskResult(False, f"「{p.suffix or '没后缀'}」这种不是文本，读不了（我只读文字）")

        size = p.stat().st_size
        if size > MAX_READ_BYTES:
            return DeskResult(False, f"文件有 {human_size(size)}，太大了，我不整份读")
        if size == 0:
            return DeskResult(True, f"「{p.name}」是空文件")

        raw_bytes = p.read_bytes()
        text = None
        for enc in ("utf-8", "utf-8-sig", "gbk"):
            try:
                text = raw_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = raw_bytes.decode("utf-8", errors="replace")

        text = mask_secrets(text)
        lines = text.splitlines()
        clipped = len(lines) > 200
        if clipped:
            lines = lines[:200]
        body = "\n".join(lines)
        note = f"（只给了前 200 行，一共 {len(text.splitlines())} 行）" if clipped else ""
        return DeskResult(True, f"「{p.name}」的内容{note}：\n{body}",
                          detail=f"读 {p.name}（{human_size(size)}）{'截断' if clipped else ''}")

    def _find_files(self, keyword: str, root: str) -> DeskResult:
        kw = (keyword or "").strip()
        if not kw:
            return DeskResult(False, "要找什么？关键词是空的")
        if len(kw) > 40:
            kw = kw[:40]

        if root:
            base, why = self.resolve(root)
            if not base:
                return DeskResult(False, f"搜不了「{root}」：{why}")
            bases = [base]
        else:
            bases = [r for r in self.allow_roots if r != self.base.resolve()] or self.allow_roots

        hits: List[str] = []
        scanned = 0
        deadline = time.time() + 8.0      # 别让一次搜索把整轮对话拖死
        low = kw.lower()
        for b in bases:
            if not b.exists():
                continue
            for dirpath, dirnames, filenames in os.walk(b):
                dirnames[:] = [d for d in dirnames
                               if d.lower() not in DENY_DIR_NAMES and not d.startswith(".")]
                for fn in filenames:
                    scanned += 1
                    if low in fn.lower():
                        full = Path(dirpath) / fn
                        try:
                            hits.append(f"{full}  ({human_size(full.stat().st_size)})")
                        except OSError:
                            hits.append(str(full))
                if len(hits) >= 30 or time.time() > deadline or scanned > 40000:
                    break
            if len(hits) >= 30 or time.time() > deadline:
                break

        if not hits:
            return DeskResult(True, f"找了 {scanned} 个文件，没找到名字带「{kw}」的",
                              detail=f"找「{kw}」，0 个（扫了 {scanned}）")
        body = "\n".join(hits[:30])
        more = "" if len(hits) < 30 else "\n（还有更多，只列了前 30 个）"
        return DeskResult(True, f"找到 {len(hits)} 个名字带「{kw}」的：\n{body}{more}",
                          detail=f"找「{kw}」，{len(hits)} 个")

    def _sys_info(self) -> DeskResult:
        import platform
        fg = _foreground_title()
        bits = [f"现在 {time.strftime('%Y-%m-%d %H:%M:%S %A')}",
                f"前台窗口是「{fg}」",
                f"系统 {platform.system()} {platform.release()}，{os.cpu_count()} 核"]
        try:
            import psutil                    # 装了就用，没装就算了
            vm = psutil.virtual_memory()
            bits.append(f"内存用了 {vm.percent:.0f}%（{human_size(vm.used)}/{human_size(vm.total)}）")
            b = psutil.sensors_battery()
            if b:
                bits.append(f"电量 {b.percent:.0f}%{'（在充电）' if b.power_plugged else ''}")
        except Exception:
            pass
        return DeskResult(True, "；".join(bits) + "。", detail="看电脑状态")

    def _trim_shots(self) -> None:
        try:
            shots = sorted(self.shot_dir.glob("shot_*.png"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for old in shots[MAX_SHOT_KEEP:]:
                old.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------- OCR

def _foreground_title() -> str:
    try:
        import win32gui
        h = win32gui.GetForegroundWindow()
        t = win32gui.GetWindowText(h) or ""
        return t.strip()[:80] or "（无标题）"
    except Exception:
        return "未知"


async def _ocr_async(pil_img, lang: str = "zh-Hans-CN") -> Tuple[List[str], str]:
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    buf = io.BytesIO()
    pil_img.save(buf, "PNG")
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream.get_output_stream_at(0))
    writer.write_bytes(buf.getvalue())
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()

    engine = OcrEngine.try_create_from_language(Language(lang))
    if engine is None:
        engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("这台机器没装 OCR 语言包")

    result = await engine.recognize_async(bitmap)
    lines = []
    for line in result.lines:
        t = "".join(w.text for w in line.words)
        if t.strip():
            lines.append(t.strip())
    return lines, result.text


def _ocr_lines(pil_img) -> Tuple[List[str], str]:
    """整屏 OCR。先放大 2 倍再识别 —— 实测小字号直接读会错字连篇。"""
    from PIL import Image, ImageEnhance, ImageOps

    try:
        big = pil_img.resize((pil_img.width * 2, pil_img.height * 2), Image.LANCZOS)
        big = ImageOps.grayscale(big)
        big = ImageEnhance.Contrast(big).enhance(1.6).convert("RGB")
        lines, text = asyncio.run(_ocr_async(big))
        return lines, ""
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


_CJK = re.compile(r"[\u4e00-\u9fff]")
_ASCII_WORD = re.compile(r"[A-Za-z]{2,}")


def _clean_ocr(lines: List[str], max_chars: int = 1400) -> str:
    """OCR 原始结果里混着图标误识别（"车讼^巨"这种），只留有意义的行。"""
    out: List[str] = []
    seen: Dict[str, int] = {}
    for raw in lines:
        t = raw.strip()
        if not t or len(t) < 2:
            continue
        cjk = len(_CJK.findall(t))
        words = _ASCII_WORD.findall(t)
        # 至少两个汉字，或者至少一个两字母以上的英文词
        if cjk < 2 and not words:
            continue
        # 汉字占比太低的（图标乱码），除非含英文单词或数字
        if len(t) >= 4 and cjk / len(t) < 0.3 and not words and not re.search(r"\d", t):
            continue
        # 同一行重复出现（聊天列表、菜单），最多留两次
        seen[t] = seen.get(t, 0) + 1
        if seen[t] > 2:
            continue
        out.append(t)

    text = "\n".join(out)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n……（后面还有，没全列）"
    return text


def build_tools_spec(enabled: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    if not enabled:
        return TOOL_SPECS
    want = set(enabled)
    return [t for t in TOOL_SPECS if t["function"]["name"] in want]
