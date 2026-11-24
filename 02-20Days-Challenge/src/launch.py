# -*- coding: utf-8 -*-
"""后台静默启动 bridge.py，不占终端、不随启动它的窗口一起退出。

start-bot.bat 是有窗口的版本，适合想看着日志跑的时候用；
这个脚本给"开机挂着、不用管它"的场景用，日志照样写 logs/bridge.log。

用法：python launch.py
"""
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
EXE = BASE / ".venv" / "Scripts" / "pythonw.exe"
if not EXE.exists():
    EXE = Path(sys.executable).with_name("pythonw.exe")

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def main() -> int:
    if not EXE.exists():
        print("找不到 pythonw.exe，请检查 .venv 是否建好")
        return 1
    cmd = [str(EXE), "bridge.py"]
    kwargs = dict(cwd=str(BASE), stdin=subprocess.DEVNULL, close_fds=True)
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        p = subprocess.Popen(cmd, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError:
        # 当前 job object 不允许 breakaway 时会走到这里，去掉这个标志再试
        p = subprocess.Popen(cmd, creationflags=flags, **kwargs)
    print(f"已在后台启动，pid={p.pid}，日志：{BASE / 'logs' / 'bridge.log'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
