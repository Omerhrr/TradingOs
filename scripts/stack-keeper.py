#!/usr/bin/env python3
"""Trading-OS local stack keeper daemon (audit sessions).

Classic double-fork daemon - survives the sandbox's per-call process
reaping (nohup/&/setsid from a tool shell all die at call end).

Keeps two services up while an audit needs them:
  - API:  uvicorn app.main:app on 127.0.0.1:8000  (cwd backend)
  - UI:   node .output/server/index.mjs on :3001  (cwd frontend, PORT=3001)

Start detached from a tool shell:
  python3 /home/z/Trading-OS/scripts/stack-keeper.py start
Stop:
  python3 /home/z/Trading-OS/scripts/stack-keeper.py stop
"""
import os
import signal
import subprocess
import sys
import time

REPO = "/home/z/Trading-OS"
LOG = f"{REPO}/stack-keeper.log"
API_CMD = ["python3", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000"]
UI_CMD = ["node", ".output/server/index.mjs"]
UI_ENV = {**os.environ, "PORT": "3001", "HOSTNAME": "127.0.0.1"}


def log(msg: str) -> None:
    line = f"[stack-keeper {time.strftime('%T')}] {msg}"
    with open(LOG, "a") as f:
        f.write(line + "\n")


def port_up(port: str) -> bool:
    try:
        out = subprocess.run(["ss", "-tln"], capture_output=True, text=True, timeout=5).stdout
        return f":{port} " in out
    except Exception:
        return True  # unsure -> do not double-spawn


def loop() -> None:
    log("daemon loop started (api :8000, ui :3001)")
    while True:
        try:
            if not port_up("8000"):
                log("starting api on :8000")
                with open(f"{REPO}/api.log", "a") as sf:
                    subprocess.Popen(
                        API_CMD, cwd=f"{REPO}/backend", stdout=sf,
                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )
            if not port_up("3001"):
                log("starting ui on :3001")
                with open(f"{REPO}/ui.log", "a") as sf:
                    subprocess.Popen(
                        UI_CMD, cwd=f"{REPO}/frontend", env=UI_ENV, stdout=sf,
                        stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )
        except Exception as e:  # noqa: BLE001
            log(f"error: {e}")
        time.sleep(5)


def daemonize() -> None:
    pid = os.fork()
    if pid == 0:
        os.setsid()
        if os.fork() != 0:
            os._exit(0)
        sys.stdout.flush()
        os.chdir(REPO)
        fd = os.open(os.devnull, os.O_RDWR)
        os.dup2(fd, 0)
        os.dup2(fd, 1)
        os.dup2(fd, 2)
    else:
        os.waitpid(pid, 0)
        print(f"stack-keeper daemonized (see {LOG})")
        sys.exit(0)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        try:
            out = subprocess.run(["pgrep", "-f", "stack-keeper.py"], capture_output=True, text=True).stdout
            for pid in out.split():
                if int(pid) != os.getpid():
                    os.kill(int(pid), signal.SIGTERM)
            print("stopped")
        except Exception as e:  # noqa: BLE001
            print(f"stop failed: {e}")
        sys.exit(0)
    daemonize()
    loop()
