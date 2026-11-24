"""desk.py 的离线测试：沙箱是否真的挡得住、工具是否真能用。"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import desk as D

BASE = Path(tempfile.mkdtemp(prefix="desk_"))
HOME = Path(os.environ["USERPROFILE"])
ok_cnt = 0


def check(name, cond, extra=""):
    global ok_cnt
    print(("  [OK] " if cond else "  [!!] ") + name + (f"  {extra}" if extra else ""))
    assert cond, name
    ok_cnt += 1


d = D.Desk(BASE, {})
print(f"允许的根：{[str(r) for r in d.allow_roots]}")
print()

# ---------------------------------------------------------------- [1] 路径沙箱
print("[1] 路径沙箱")
cases = [
    ("桌面", True, "中文别名"),
    ("桌面/随便.txt", True, "别名+子路径"),
    (str(BASE), True, "项目目录"),
    ("C:/Windows/System32", False, "系统目录"),
    (str(HOME / "AppData/Roaming"), False, "AppData"),
    (str(HOME / ".ssh"), False, ".ssh"),
    ("C:/Program Files", False, "Program Files"),
    ("D:/随便一个不存在的目录", False, "不在白名单"),
]
for raw, should_ok, name in cases:
    p, why = d.resolve(raw)
    got = p is not None
    check(f"{name:14} {raw[:40]:<42} {'允许' if got else '拒绝:' + why[:18]}", got == should_ok)
print()

# ---------------------------------------------------------------- [2] 越权不执行
print("[2] 越权调用不会真的读")
r = d.execute("read_file", {"path": "C:/Windows/System32/drivers/etc/hosts"})
check("读 hosts 被拒", not r.ok, r.text[:40])
r = d.execute("list_dir", {"path": str(HOME / "AppData")})
check("列 AppData 被拒", not r.ok, r.text[:40])

# 项目 config.json 在允许范围内，但里面的 api_key 必须打码
cfg_path = Path("config.json").resolve()
if cfg_path.exists():
    d_proj = D.Desk(Path(".").resolve(), {})   # 换成项目根，模拟真实运行
    r = d_proj.execute("read_file", {"path": "config.json"})
    key = json.load(open("config.json", encoding="utf-8"))["deepseek"]["api_key"]
    check("能读自己目录的 config.json", r.ok, r.text[:40])
    check("api_key 已打码（不出现在结果里）", key not in r.text, "真实密钥未泄漏")
    check("打码后仍有 *** 痕迹", "***" in r.text)
print()

# ---------------------------------------------------------------- [3] 列目录
print("[3] 列目录")
(BASE / "演示夹").mkdir(exist_ok=True)
(BASE / "笔记.txt").write_text("第一行\n第二行\n", encoding="utf-8")
(BASE / ".env").write_text("SECRET=1", encoding="utf-8")
r = d.execute("list_dir", {"path": str(BASE)})
check("列出笔记.txt", "笔记.txt" in r.text, r.detail)
check("隐藏文件 .env 不出现", ".env" not in r.text)
r = d.execute("list_dir", {"path": str(BASE / "不存在的夹")})
check("不存在的目录给出明确提示", not r.ok and "不存在" in r.text, r.text[:30])
print()

# ---------------------------------------------------------------- [4] 读文件
print("[4] 读文件")
r = d.execute("read_file", {"path": "笔记.txt"})
check("相对路径按项目目录解析", r.ok and "第一行" in r.text, r.text[:30])
(BASE / "图.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
r = d.execute("read_file", {"path": "图.png"})
check("二进制文件被拒", not r.ok and "不是文本" in r.text, r.text[:30])
(BASE / "big.txt").write_text("x" * (200 * 1024), encoding="utf-8")
r = d.execute("read_file", {"path": "big.txt"})
check("超大文件被拒", not r.ok and "太大" in r.text, r.text[:30])
(BASE / "长.txt").write_text("\n".join(str(i) for i in range(300)), encoding="utf-8")
r = d.execute("read_file", {"path": "长.txt"})
check("超长文件截断到 200 行", r.ok and "只给了前 200 行" in r.text, r.detail)
os.remove(BASE / "big.txt")

# 伪造的密钥内容也要打码
(BASE / "泄漏.txt").write_text(
    'api_key = "sk-FAKEKEYFORTESTONLY1234"\ntoken: abcdefgh12345678', encoding="utf-8")
r = d.execute("read_file", {"path": "泄漏.txt"})
check("文件里的 sk- 密钥被打码", "sk-FAKEKEYFORTESTONLY" not in r.text, r.text[:60])
check("文件里的 token 被打码", "abcdefgh12345678" not in r.text)
print()

# ---------------------------------------------------------------- [5] 找文件
print("[5] 找文件")
(BASE / "作品集").mkdir(exist_ok=True)
(BASE / "作品集" / "终稿_作品集说明.md").write_text("x", encoding="utf-8")
r = d.execute("find_files", {"keyword": "作品集", "root": str(BASE)})
check("找到带关键词的文件", r.ok and "终稿_作品集说明.md" in r.text, r.detail)
r = d.execute("find_files", {"keyword": "根本不存在的东西xyz", "root": str(BASE)})
check("找不到时明确说没找到", r.ok and "没找到" in r.text, r.text[:40])
r = d.execute("find_files", {"keyword": ""})
check("空关键词被拒", not r.ok)
print()

# ---------------------------------------------------------------- [6] 系统信息
print("[6] 系统信息")
r = d.execute("sys_info", {})
check("拿到时间与前台窗口", r.ok and "现在" in r.text and "前台窗口" in r.text, r.text[:80])
print()

# ---------------------------------------------------------------- [7] 看屏幕
print("[7] 看屏幕（真实截屏 + OCR）")
r = d.execute("look_screen", {})
check("截屏成功", r.ok, r.detail)
check("生成了截图文件", bool(r.shot) and (BASE / "desk_shots" / r.shot).exists(), r.shot)
check("OCR 读到一些文字", len(r.text) > 60, f"{len(r.text)} 字")
print("   前 6 行：")
for line in r.text.splitlines()[1:7]:
    print("     ", line[:64])
print()

# ---------------------------------------------------------------- [8] 流水与预览
print("[8] 流水记录与批准预览")
check("流水记下了每次调用", len(d.history) >= 8, f"{len(d.history)} 条")
check("流水含耗时", all("ms" in h for h in d.history))
check("预览语句是人话", "屏幕" in d.preview("look_screen", {})
      and "笔记.txt" in d.preview("read_file", {"path": "笔记.txt"})
      and "作品集" in d.preview("find_files", {"keyword": "作品集"}), "三种预览都对得上")
check("未知工具被拒", not d.execute("rm_rf", {}).ok)

# 截图数量上限
for _ in range(3):
    d.execute("look_screen", {})
n_shots = len(list((BASE / "desk_shots").glob("shot_*.png")))
check("截图文件不会无限堆积", n_shots <= D.MAX_SHOT_KEEP, f"{n_shots} 张")
print()

# ---------------------------------------------------------------- [9] 工具 schema
print("[9] 给模型的工具定义")
specs = D.build_tools_spec()
names = [s["function"]["name"] for s in specs]
check("五个工具齐全", names == ["look_screen", "list_dir", "read_file", "find_files", "sys_info"], str(names))
for s in specs:
    f = s["function"]
    assert f["description"] and f["parameters"]["type"] == "object"
check("每个工具都有描述和参数", True)
check("可以按需裁剪", len(D.build_tools_spec(["look_screen"])) == 1)

import shutil
shutil.rmtree(BASE, ignore_errors=True)
print()
print(f"全部通过：{ok_cnt} 项")
