# -*- coding: utf-8 -*-
"""Daemon 启动器：双重 fork 脱离当前进程组，让 app.py 不随沙箱 shell 回收。
用法: /opt/homebrew/anaconda3/bin/python3 daemon_launcher.py
"""
import os
import pathlib
import sys
import time

APP_DIR = str(pathlib.Path(__file__).resolve().parent)
PY = os.path.join(APP_DIR, ".venv", "bin", "python") if os.path.exists(os.path.join(APP_DIR, ".venv", "bin", "python")) else sys.executable
LOG = "/tmp/market-monitor.log"
PIDFILE = "/tmp/market-monitor.pid"


def daemonize():
    # 第一次 fork：父进程退出
    if os.fork() > 0:
        os._exit(0)
    os.setsid()          # 新会话，脱离控制终端和进程组
    # 第二次 fork：防止重新获取控制终端
    if os.fork() > 0:
        os._exit(0)
    os.chdir(APP_DIR)
    os.umask(0)


def main():
    daemonize()
    # 重定向 stdio
    fd = os.open(LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(fd, 0)
    os.dup2(fd, 1)
    os.dup2(fd, 2)
    if fd > 2:
        os.close(fd)
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))
    os.execv(PY, [PY, "-u", "app.py"])


if __name__ == "__main__":
    main()
