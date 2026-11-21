import ctypes
import re
import time

import win32api
import win32con
import win32gui
import win32process

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except (AttributeError, OSError):
    ctypes.windll.user32.SetProcessDPIAware()


def _process_name(hwnd):
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        handle = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        try:
            return win32process.GetModuleFileNameEx(handle, 0).rsplit("\\", 1)[-1]
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        return "?"


def _describe(hwnd):
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    return {
        "hwnd": hwnd,
        "title": win32gui.GetWindowText(hwnd),
        "rect": (left, top, right - left, bottom - top),
        "minimized": bool(win32gui.IsIconic(hwnd)),
        "process": _process_name(hwnd),
    }


def list_windows(pattern=None):
    regex = re.compile(pattern, re.IGNORECASE) if pattern else None
    found = []

    def collect(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title.strip() or (regex and not regex.search(title)):
            return True
        found.append(_describe(hwnd))
        return True

    win32gui.EnumWindows(collect, None)
    return found


def parse_hwnd(value):
    try:
        return int(value, 0)
    except ValueError as exc:
        raise ValueError("窗口句柄要写成十进制或 0x 开头，例如 0x001A0048") from exc


def get_window(hwnd):
    if not win32gui.IsWindow(hwnd):
        raise LookupError(f"窗口句柄 {hwnd} 不存在")
    return _describe(hwnd)


def client_box(hwnd):
    left, top = win32gui.ClientToScreen(hwnd, (0, 0))
    width, height = win32gui.GetClientRect(hwnd)[2:]
    return left, top, width, height


def is_foreground(hwnd):
    fg = win32gui.GetForegroundWindow()
    if not fg:
        return False
    return win32gui.GetAncestor(fg, win32con.GA_ROOT) == win32gui.GetAncestor(hwnd, win32con.GA_ROOT)


def ensure_visible(hwnd):
    """恢复最小化的窗口，但不抢前台焦点。"""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        time.sleep(0.3)


def focus_window(hwnd):
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    if win32gui.GetForegroundWindow() == hwnd:
        return True

    cur_tid = win32api.GetCurrentThreadId()
    fg = win32gui.GetForegroundWindow()
    attached = []
    try:
        for tid in {win32process.GetWindowThreadProcessId(hwnd)[0],
                    win32process.GetWindowThreadProcessId(fg)[0] if fg else 0}:
            if tid and tid != cur_tid:
                try:
                    win32process.AttachThreadInput(cur_tid, tid, True)
                    attached.append(tid)
                except Exception:
                    pass
        win32gui.BringWindowToTop(hwnd)
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    finally:
        for tid in attached:
            try:
                win32process.AttachThreadInput(cur_tid, tid, False)
            except Exception:
                pass

    time.sleep(0.15)
    return win32gui.GetForegroundWindow() == hwnd
