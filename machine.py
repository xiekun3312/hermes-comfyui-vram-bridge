"""Local Windows service and VRAM checks."""
import csv
import ctypes
from ctypes import wintypes
import json
import logging
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request


class HttpError(RuntimeError):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {json.dumps(body, ensure_ascii=False)[:3000]}")


def request(port, route, payload=None, timeout=5):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}", data, {"Content-Type": "application/json"}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as reply:
            return json.load(reply)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw)
        except ValueError:
            body = raw.decode("utf-8", errors="replace")
        raise HttpError(exc.code, body) from exc


def hidden(*args, timeout=15):
    return subprocess.run(
        args, capture_output=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW,
    )


def visible_console(args, cwd, env=None):
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 4  # SW_SHOWNOACTIVATE
    return subprocess.Popen(
        args, cwd=cwd, env=env, startupinfo=startup, creationflags=subprocess.CREATE_NEW_CONSOLE,
    )


def listening(port):
    result = hidden("netstat.exe", "-ano")
    if result.returncode:
        raise RuntimeError(f"netstat failed: {result.stderr.strip()}")
    owners = set()
    for row in result.stdout.splitlines():
        fields = row.split()
        if len(fields) != 5 or fields[0] != "TCP":
            continue
        if fields[1].endswith(f":{port}") and fields[2] in {"0.0.0.0:0", "[::]:0"}:
            owners.add(int(fields[4]))
    return owners


def process_info(pid):
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    cmd = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "$ErrorActionPreference='Stop'; "
        f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}'; "
        "if ($p) { @{path=$p.ExecutablePath; command=$p.CommandLine; "
        "created=$p.CreationDate.ToFileTimeUtc().ToString()} | ConvertTo-Json -Compress }"
    )
    result = hidden(str(shell), "-NoProfile", "-NonInteractive", "-Command", cmd)
    if result.returncode:
        raise RuntimeError(f"无法确认进程 {pid} 身份：{result.stderr.strip()}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def same_path(a, b):
    return bool(a) and os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def stop_verified_process(pid, info):
    """Terminate only the original process, even if its PID is reused."""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000 | 0x0001 | 0x00100000, False, pid)
    if not handle:
        if ctypes.get_last_error() == 87:
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        created, exited, user, system = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, created, exited, user, system):
            raise ctypes.WinError(ctypes.get_last_error())
        stamp = (created.dwHighDateTime << 32) | created.dwLowDateTime
        if abs(stamp - int(info["created"])) >= 10:  # CIM has microsecond precision.
            raise RuntimeError(f"进程 {pid} 身份已改变")
        if not kernel.TerminateProcess(handle, 0) and kernel.WaitForSingleObject(handle, 0) != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        if kernel.WaitForSingleObject(handle, 15_000) != 0:
            raise RuntimeError(f"进程 {pid} 未在 15 秒内退出")
    finally:
        kernel.CloseHandle(handle)


class Machine:
    def __init__(self, config):
        self.c = config
        self.comfy_root = Path(config["comfy_root"])
        self.comfy_python = self.comfy_root / "python_embeded/python.exe"
        self.comfy_main = self.comfy_root / "ComfyUI/main.py"
        self.llm_bat = Path(config["llm_bat"])
        self.llm_exe = self.llm_bat.parent / "llama.cpp/llama-server.exe"
        self.llm_port = config["llm_port"]
        self.comfy_port = config["comfy_port"]
        self.llm_process = None

    def check_files(self):
        for path in (self.comfy_python, self.comfy_main, self.llm_bat, self.llm_exe):
            if not path.is_file():
                raise RuntimeError(f"文件不存在：{path}")

    def gpu(self):
        result = hidden(
            "nvidia-smi.exe", "-i", str(self.c["gpu_index"]),
            "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits",
        )
        if result.returncode:
            raise RuntimeError(f"nvidia-smi 失败：{result.stderr.strip() or result.stdout.strip()}")
        used, total = map(int, next(csv.reader(result.stdout.splitlines())))
        return {"used_mib": used, "total_mib": total}

    def wait_for_vram(self):
        deadline = time.monotonic() + self.c["vram_wait_seconds"]
        consecutive = 0
        sample = None
        last_error = ""
        while time.monotonic() < deadline:
            try:
                sample = self.gpu()
                consecutive = consecutive + 1 if sample["used_mib"] <= self.c["max_idle_vram_mib"] else 0
                if consecutive == 2:
                    logging.info("显存已释放，当前使用 %s MiB", sample["used_mib"])
                    return
            except (OSError, RuntimeError, ValueError, StopIteration, subprocess.TimeoutExpired) as exc:
                consecutive = 0
                last_error = str(exc)
            time.sleep(2)
        raise RuntimeError(f"等待显存释放超时。最近读数 {sample}。{last_error}")

    def llm_owner(self):
        owners = listening(self.llm_port)
        if not owners:
            return None
        if len(owners) != 1:
            raise RuntimeError(f"LLM 端口 {self.llm_port} 有多个占用进程：{sorted(owners)}")
        pid = next(iter(owners))
        info = process_info(pid)
        cmd = (info or {}).get("command") or ""
        if not info or not same_path(info["path"], self.llm_exe) or self.c["llm_alias"] not in cmd:
            raise RuntimeError(f"LLM 端口 {self.llm_port} 被其他进程占用，PID {pid}")
        return pid, info

    def stop_llm(self, owner):
        if owner:
            pid, info = owner
            logging.info("关闭 LLM PID %s，端口 %s", pid, self.llm_port)
            stop_verified_process(pid, info)
        self.wait_port_closed(self.llm_port)

    def wait_port_closed(self, port):
        for _ in range(15):
            if not listening(port):
                return
            time.sleep(1)
        raise RuntimeError(f"端口 {port} 未释放")

    def llm_ready(self):
        try:
            return request(self.llm_port, "/health").get("status") == "ok"
        except (HttpError, OSError, ValueError):
            return False

    def wait_ready(self, label, probe, seconds, process=None):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if process is not None and process.poll() is not None:
                raise RuntimeError(f"{label} 启动进程已退出，退出码 {process.returncode}")
            if probe():
                return
            time.sleep(2)
        raise RuntimeError(f"{label} 未在 {seconds} 秒内就绪")

    def start_llm(self):
        if listening(self.comfy_port):
            raise RuntimeError(f"ComfyUI 端口 {self.comfy_port} 仍在监听")
        owner = self.llm_owner()
        if not owner and (self.llm_process is None or self.llm_process.poll() is not None):
            self.wait_for_vram()
            if listening(self.comfy_port):
                raise RuntimeError(f"ComfyUI 端口 {self.comfy_port} 已被占用")
            self.llm_process = visible_console(
                [os.environ["ComSpec"], "/d", "/c", "call", str(self.llm_bat)],
                cwd=self.llm_bat.parent,
            )
        self.wait_ready("LLM", self.llm_ready, self.c["llm_start_seconds"], None if owner else self.llm_process)
        if not self.llm_owner():
            raise RuntimeError("LLM 健康检查通过后，监听进程已退出")

    def start_comfy(self):
        if listening(self.comfy_port) or listening(self.llm_port):
            raise RuntimeError("启动 ComfyUI 前发现 ComfyUI 或 LLM 端口仍被占用")
        env = dict(os.environ, POLARS_SKIP_CPU_CHECK="1", PYTHONUTF8="1")
        cmd = [
            str(self.comfy_python), "-s", str(self.comfy_main),
            "--windows-standalone-build", "--disable-auto-launch", "--lowvram", "--port", str(self.comfy_port),
            "--listen", "127.0.0.1", "--multi-user",
        ]
        return visible_console(cmd, cwd=self.comfy_root, env=env)

    def wait_comfy(self, process):
        def ready():
            try:
                return bool(request(self.comfy_port, "/system_stats")["devices"])
            except (HttpError, OSError, ValueError, KeyError):
                return False

        self.wait_ready("ComfyUI", ready, self.c["comfy_start_seconds"], process)
        if listening(self.comfy_port) != {process.pid}:
            raise RuntimeError("ComfyUI 端口的监听者不是本次启动的 Python 进程")

    def stop_comfy(self, process=None, saved_pid=None, saved_info=None):
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=20)
        elif process is None and saved_pid and saved_info:
            info = process_info(saved_pid)
            if info and info["created"] == saved_info["created"] and same_path(info["path"], self.comfy_python):
                stop_verified_process(saved_pid, info)
        self.wait_port_closed(self.comfy_port)
