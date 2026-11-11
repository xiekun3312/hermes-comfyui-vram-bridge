"""Submit the wake message to a fresh Hermes Desktop conversation."""
from contextlib import closing
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sqlite3
import time

from machine import process_info


TEXT = "醒来"
EVENT_E_ALL_SUBSCRIBERS_FAILED = -2147220991  # 0x80040201
EMPTY_COMPOSER = {
    "", "我们要构建什么？", "给 Hermes 一个任务", "你在想什么？", "描述你需要什么",
    "我们该处理什么？", "随便问点什么", "从一个目标开始",
    "What are we building?", "Give Hermes a task", "What's on your mind?",
    "Describe what you need", "What should we tackle?", "Ask anything", "Start with a goal",
}


def hermes_db():
    home = os.environ.get("HERMES_HOME") or Path(os.environ["LOCALAPPDATA"]) / "hermes"
    return Path(home) / "state.db"


def cursor():
    with closing(sqlite3.connect(hermes_db().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        return db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]


def confirmation(after_id, since):
    with closing(sqlite3.connect(hermes_db().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        row = db.execute(
            """SELECT s.id FROM sessions s JOIN messages m ON m.session_id = s.id
               WHERE s.source IN ('desktop', 'gui', 'local') AND s.started_at >= ?
                 AND m.id > ? AND m.role = 'user' AND m.content = ?
                 AND NOT EXISTS (
                     SELECT 1 FROM messages older
                     WHERE older.session_id = s.id AND older.role = 'user' AND older.id < m.id
                 )
               ORDER BY m.id LIMIT 1""",
            (since - 2, after_id, TEXT),
        ).fetchone()
    return row[0] if row else None


def wait_for(probe, seconds, message):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(0.2)
    raise RuntimeError(message)


def focus_window(hwnd, executable):
    user = ctypes.windll.user32
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user.IsIconic.argtypes = [wintypes.HWND]
    user.SetForegroundWindow.argtypes = [wintypes.HWND]
    user.BringWindowToTop.argtypes = [wintypes.HWND]
    user.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
    if user.IsIconic(hwnd):
        user.ShowWindow(hwnd, 9)
    user.SetForegroundWindow(hwnd)
    if user.GetForegroundWindow() != hwnd:
        # The running Electron instance handles a second launch by activating its window.
        os.startfile(executable)
        for _ in range(10):
            if user.GetForegroundWindow() == hwnd:
                return user
            time.sleep(0.2)
    if user.GetForegroundWindow() != hwnd:
        user.PeekMessageW(ctypes.byref(wintypes.MSG()), None, 0, 0, 0)
        foreground = user.GetWindowThreadProcessId(user.GetForegroundWindow(), None)
        current = ctypes.windll.kernel32.GetCurrentThreadId()
        if foreground and foreground != current and user.AttachThreadInput(current, foreground, True):
            try:
                user.BringWindowToTop(hwnd)
                user.SetForegroundWindow(hwnd)
            finally:
                user.AttachThreadInput(current, foreground, False)
    if user.GetForegroundWindow() != hwnd:
        user.SwitchToThisWindow(hwnd, True)
        time.sleep(0.3)
    if user.GetForegroundWindow() != hwnd:
        # Windows unlocks a foreground switch after an Alt keystroke.
        user.keybd_event(0x12, 0, 0, 0)
        user.keybd_event(0x12, 0, 2, 0)
        user.SetForegroundWindow(hwnd)
        time.sleep(0.3)
    if user.GetForegroundWindow() != hwnd:
        raise RuntimeError("Hermes 没有取得前台焦点，请将 Hermes 窗口切到前台")
    return user


def send(mark_sending):
    import uiautomation as auto

    since = time.time()
    after_id = cursor()
    with auto.UIAutomationInitializerInThread():
        desktop = auto.GetRootControl()
        windows = []
        for control in desktop.GetChildren():
            if control.Name == "Hermes" and control.ClassName == "Chrome_WidgetWin_1":
                info = process_info(control.ProcessId)
                if info and info["path"] and Path(info["path"]).name.lower() == "hermes.exe":
                    windows.append((control, info["path"]))
        if len(windows) != 1:
            raise RuntimeError(f"预期找到一个 Hermes 主窗口，找到 {len(windows)} 个")
        window, executable = windows[0]
        hwnd = window.NativeWindowHandle
        user = focus_window(hwnd, executable)

        def ensure_foreground():
            if user.GetForegroundWindow() != hwnd:
                raise RuntimeError("Hermes 窗口焦点已改变，暂停唤醒")

        document = window.DocumentControl(AutomationId="RootWebArea", searchDepth=12)

        def fresh_route():
            if not document.Exists(0, 0):
                return False
            route = document.GetValuePattern().Value
            return route.rsplit("#", 1)[-1] == "/"

        ensure_foreground()
        auto.SendKeys("{Ctrl}n", waitTime=0.1)
        wait_for(fresh_route, 10, "Ctrl+N 后未确认 Hermes 空白会话")
        # React reuses the composer while the new tab mounts. Its accessibility
        # value can expose one of several starter placeholders as if it were text.
        edit = document.EditControl(
            searchDepth=40, Compare=lambda control, _: control.Name in {"消息", "Message"}
        )
        send_button = document.ButtonControl(
            searchDepth=40, Compare=lambda control, _: control.Name in {"发送", "发送消息", "Send", "Send message"}
        )

        def composer_empty():
            if not fresh_route() or not edit.Exists(0, 0):
                return False
            return edit.GetValuePattern().Value.strip() in EMPTY_COMPOSER

        wait_for(composer_empty, 10, "未确认 Hermes 新会话的空白输入框")
        ensure_foreground()
        if send_button.Exists(0, 0) and send_button.IsEnabled:
            raise RuntimeError("Hermes 新会话已有可发送的草稿")
        edit.SetFocus()
        if not edit.HasKeyboardFocus:
            raise RuntimeError("无法聚焦 Hermes 消息输入框")
        ensure_foreground()
        auto.SendKeys(TEXT, waitTime=0.15)
        wait_for(
            lambda: edit.GetValuePattern().Value.strip() == TEXT
            and send_button.Exists(0, 0) and send_button.IsEnabled,
            12, "Hermes 未显示可发送的完整“醒来”草稿",
        )
        ensure_foreground()
        if not fresh_route() or edit.GetValuePattern().Value.strip() != TEXT:
            raise RuntimeError("提交前 Hermes 的会话或输入框焦点发生了改变")
        invoke = send_button.GetInvokePattern()
        if not invoke:
            raise RuntimeError("Hermes 的发送按钮未提供可调用的操作")
        mark_sending(after_id, since)
        ensure_foreground()
        invoke.Invoke()

    return wait_for(
        lambda: confirmation(after_id, since), 90,
        "已调用 Hermes 的发送按钮，正在等待新会话消息写入",
    )
