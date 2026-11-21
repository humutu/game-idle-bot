import ctypes
import random
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

VK_F8 = 0x77
VK_F9 = 0x78

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001

user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.PostMessageW.restype = wintypes.BOOL

_NAMED_KEYS = {
    "space": 0x20,
    "enter": 0x0D,
    "esc": 0x1B,
    "tab": 0x09,
    "backspace": 0x08,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "insert": 0x2D,
    "delete": 0x2E,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}

_EXTENDED = {0x26, 0x28, 0x25, 0x27, 0x24, 0x23, 0x21, 0x22, 0x2D, 0x2E}


def _send(*events):
    array = (INPUT * len(events))(*events)
    sent = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise OSError(f"SendInput 失败，错误码 {ctypes.get_last_error()}")


def _mouse_event(flags):
    event = INPUT()
    event.type = INPUT_MOUSE
    event.mi.dx = 0
    event.mi.dy = 0
    event.mi.mouseData = 0
    event.mi.dwFlags = flags
    event.mi.time = 0
    event.mi.dwExtraInfo = 0
    return event


def click(x, y, jitter_px=2):
    if jitter_px:
        x += random.randint(-jitter_px, jitter_px)
        y += random.randint(-jitter_px, jitter_px)
    user32.SetCursorPos(int(x), int(y))
    time.sleep(random.uniform(0.02, 0.06))
    _send(_mouse_event(MOUSEEVENTF_LEFTDOWN))
    time.sleep(random.uniform(0.05, 0.11))
    _send(_mouse_event(MOUSEEVENTF_LEFTUP))


def click_bg(hwnd, x, y, jitter_px=2):
    """不移动鼠标、不切换前台，直接给窗口发鼠标消息（窗口可以被遮挡）。

    有些小游戏客户端是 Chromium 壳，鼠标输入由顶层窗口的 WndProc 处理，
    所以消息发给顶层窗口，而不是里面的渲染子窗口。
    """
    if jitter_px:
        x += random.randint(-jitter_px, jitter_px)
        y += random.randint(-jitter_px, jitter_px)
    position = (int(y) << 16) | (int(x) & 0xFFFF)
    user32.PostMessageW(hwnd, WM_MOUSEMOVE, 0, position)
    time.sleep(random.uniform(0.02, 0.06))
    # 这类壳窗口收到带 MK_LBUTTON 的按下消息时会把自己提到前台；wParam 传 0 仍是正常点击。
    user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 0, position)
    time.sleep(random.uniform(0.05, 0.11))
    user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, position)


def parse_key(name):
    key = name.strip().lower()
    if key in _NAMED_KEYS:
        return _NAMED_KEYS[key]
    if len(key) == 1 and key.isalnum():
        return ord(key.upper())
    raise ValueError(f"不支持的按键：{name!r}（可用：字母、数字、空格、回车、方向键、F1-F12 等）")


def press(name):
    vk = parse_key(name)
    flags = KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0

    down = INPUT()
    down.type = INPUT_KEYBOARD
    down.ki.wVk = vk
    down.ki.wScan = 0
    down.ki.dwFlags = flags
    down.ki.time = 0
    down.ki.dwExtraInfo = 0

    up = INPUT()
    up.type = INPUT_KEYBOARD
    up.ki.wVk = vk
    up.ki.wScan = 0
    up.ki.dwFlags = flags | KEYEVENTF_KEYUP
    up.ki.time = 0
    up.ki.dwExtraInfo = 0

    _send(down)
    time.sleep(random.uniform(0.03, 0.08))
    _send(up)


class Hotkeys:
    def __init__(self):
        self._prev = {}

    def pressed(self, vk):
        down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
        was_down = self._prev.get(vk, False)
        self._prev[vk] = down
        return down and not was_down
