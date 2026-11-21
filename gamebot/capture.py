import ctypes

import mss
import numpy as np
import win32gui
import win32ui

from .winutil import client_box

PW_RENDERFULLCONTENT = 2


class Capturer:
    def __init__(self, background=False):
        self.background = background
        self._sct = None if background else mss.mss()

    def grab(self, hwnd):
        if self.background:
            return self._grab_background(hwnd)
        left, top, width, height = client_box(hwnd)
        if width <= 0 or height <= 0:
            raise RuntimeError("窗口客户区为空，可能被最小化")
        shot = self._sct.grab({"left": left, "top": top, "width": width, "height": height})
        frame = np.asarray(shot, dtype=np.uint8)
        return frame[:, :, :3].copy()

    def _grab_background(self, hwnd):
        if win32gui.IsIconic(hwnd):
            raise RuntimeError("窗口已最小化，后台模式需要窗口处于恢复状态")
        width, height = win32gui.GetClientRect(hwnd)[2:]
        if width <= 0 or height <= 0:
            raise RuntimeError("窗口客户区为空，无法截图")

        window_dc = win32gui.GetWindowDC(hwnd)
        source = win32ui.CreateDCFromHandle(window_dc)
        memory = source.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        try:
            bitmap.CreateCompatibleBitmap(source, width, height)
            memory.SelectObject(bitmap)
            if not ctypes.windll.user32.PrintWindow(hwnd, memory.GetSafeHdc(), PW_RENDERFULLCONTENT):
                raise RuntimeError("PrintWindow 截图失败")
            info = bitmap.GetInfo()
            bits = bitmap.GetBitmapBits(True)
            frame = np.frombuffer(bits, dtype=np.uint8).reshape(info["bmHeight"], info["bmWidth"], 4)
            return frame[:, :, :3].copy()
        finally:
            win32gui.DeleteObject(bitmap.GetHandle())
            memory.DeleteDC()
            source.DeleteDC()
            win32gui.ReleaseDC(hwnd, window_dc)

    def close(self):
        if self._sct is not None:
            self._sct.close()
