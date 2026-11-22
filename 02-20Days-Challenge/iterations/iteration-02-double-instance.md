# 迭代 02 · 双实例冲突：同一条消息被回复两遍

- 日期：2026-11-21
- 类型：并发缺陷（严重）
- 影响文件：`src/bridge.py`（新增 `acquire_single_instance`）、`src/start-bot.bat`

## 现象

用户反馈"有时候会收到两条不一样的回复"。日志里同一个时间窗内出现两次处理：

```
16:18:46 收到 主号A(wxid_MAIN_MASKED): 现在感觉怎么样
16:18:47 收到 主号A(wxid_MAIN_MASKED): 现在感觉怎么样   ← 同一条被读了两次
16:18:52 已回复 主号A: 嗯……我这边挺闲的，刚刷了会儿图。你呢，今天过得咋样？
16:19:04 已回复 主号A: 挺好的呀，刚吃饱饭有点懒洋洋的～你呢，今天过得咋样？
```

两条回复语义不同，说明不是同一次生成的重发，而是**两次独立的模型调用**。

## 定位

查进程而不是查代码：

```
pid=9208   启动于 15:49:37   cmdline: ...python.exe bridge.py
pid=19556  启动于 15:58:32   cmdline: ...python.exe bridge.py
```

两个实例相距 9 分钟启动。回溯发现：15:58 为了加载知识库重启过一次，当时的停止操作只终止了 venv 的 `python.exe` 包装进程，**真实的解释器进程还在跑**。旧实例没有知识库、新实例有，两者都盯着同一个微信库。

后果有三层：

1. 每条消息被两个实例各处理一次 → 用户收到双份回复
2. 两个进程同时读写 `state.json`（水位记录）→ 后写的覆盖先写的，水位错乱
3. 旧实例没有人设和知识库，回复风格不一致，但日志混在一起，排查时极易误判

## 改动

在 `main()` 入口加单实例锁：

```python
def acquire_single_instance() -> bool:
    lock = BASE / "bot.pid"
    if lock.exists():
        pid = int(lock.read_text().strip() or 0)
        p = psutil.Process(pid)
        if p.is_running() and "bridge.py" in " ".join(p.cmdline()):
            log.error("已经有一个实例在运行（pid=%s）……本次启动取消", pid)
            return False
    lock.write_text(str(os.getpid()))
    return True
```

配套：`finally` 里清理 pid 文件；`start-bot.bat` 对退出码 2 给"已有实例在运行"提示。

## 验证

```
16:27:56 ERROR 已经有一个实例在运行（pid=21132）。同时开两个会把每条消息回
              两遍，本次启动取消。要重启请先关掉原来的窗口。
退出码=2
```

## 踩到的坑

第一版检测写成"cmdline 里是否包含 `bridge.py` 字符串"，结果把自己也判成了实例——因为 Python 的 `-c "..."` 内联脚本参数里就带着这个字符串。改成判断"cmdline 前两个参数中是否有以 `bridge.py` 结尾的项"才准确。

这个坑值得记：**用进程名/命令行做单实例检测时，必须排除检测脚本自身。**

## 遗留

- 锁机制是"检查后写入"，两个进程同时启动仍有毫秒级竞态窗口，只是概率极低
- 更稳的做法是文件锁（`msvcrt.locking`）或命名互斥体，本次未做
