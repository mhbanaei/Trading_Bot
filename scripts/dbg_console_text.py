"""Read the visible text of a Windows console window by title.

Diagnostic helper (not part of the bot): ``AttachConsole`` to the console
window that owns the bot and dump the visible rows with
``ReadConsoleOutputCharacterW``. Used to verify what the real CMD window
shows without a screenshot — e.g. that the quiet window prints only the
startup banner, and that dropped-connection tracebacks are gone.

Usage:
    python scripts/dbg_console_text.py "Nobitex Smart Trading Bot" [rows]
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

STD_OUTPUT_HANDLE = -11


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", wintypes.WORD), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]


def find_window(needle: str) -> int:
    """Top-level window whose title CONTAINS `needle` (elevated cmd windows
    are titled «Administrator:  <title>», so an exact match misses them)."""
    user32 = ctypes.windll.user32
    found = []

    def _cb(hwnd, _lparam):
        n = user32.GetWindowTextLengthW(hwnd)
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if needle.lower() in buf.value.lower():
                found.append(hwnd)
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int,
                                          ctypes.c_void_p)(_cb), 0)
    return found[0] if found else 0


def read_window(title: str, rows: int = 60) -> list[str]:
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = find_window(title)
    if not hwnd:
        raise SystemExit("window not found: %r" % title)
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    kernel32.FreeConsole()
    if not kernel32.AttachConsole(pid.value):
        raise SystemExit("AttachConsole failed for pid %d" % pid.value)
    # after AttachConsole the inherited std handles are not the console's —
    # open CONOUT$ directly to read the buffer
    GENERIC_READ_WRITE = 0xC0000000
    FILE_SHARE_READ_WRITE = 0x3
    OPEN_EXISTING = 3
    h = kernel32.CreateFileW("CONOUT$", GENERIC_READ_WRITE,
                             FILE_SHARE_READ_WRITE, None, OPEN_EXISTING,
                             0, None)
    if h == -1:
        h = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
    info = CONSOLE_SCREEN_BUFFER_INFO()
    if not kernel32.GetConsoleScreenBufferInfo(h, ctypes.byref(info)):
        raise SystemExit("GetConsoleScreenBufferInfo failed")
    cols = max(1, info.dwSize.X)
    last = min(info.dwSize.Y, info.srWindow.Bottom + 1)
    first = max(0, last - rows)
    read = kernel32.ReadConsoleOutputCharacterW
    read.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD,
                     COORD, ctypes.POINTER(wintypes.DWORD)]
    buf = ctypes.create_unicode_buffer(cols + 1)
    n = wintypes.DWORD()
    out = []
    for y in range(first, last):
        if read(h, buf, cols, COORD(0, y), ctypes.byref(n)):
            out.append(buf[:n.value].rstrip())
    return out


def main() -> None:
    title = sys.argv[1] if len(sys.argv) > 1 else "Nobitex Smart Trading Bot"
    rows = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    for i, line in enumerate(read_window(title, rows)):
        print("%02d| %s" % (i, line))


if __name__ == "__main__":
    main()
