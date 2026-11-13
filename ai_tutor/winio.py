"""
Low-level Windows helpers (pure ctypes, no pip packages):
  * SendInput keyboard injection (tagged so our own hook can ignore it)
  * Clipboard read / write (kept out of Win+V clipboard history)
  * Foreground window / process information
  * Global low-level keyboard + mouse hooks
"""
import ctypes
import os
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t

# Every keystroke we inject carries this marker so the hook can tell it apart
# from the real keyboard.
INJECT_MARK = 0x5A17_7070

# ---------------------------------------------------------------- constants
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0104, 0x0105
WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN = 0x0201, 0x0204, 0x0207
WM_QUIT = 0x0012
LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
HWND_MESSAGE = wintypes.HWND(-3)

VK = {
    "ctrl": 0x11, "lctrl": 0xA2, "rctrl": 0xA3,
    "shift": 0x10, "lshift": 0xA0, "rshift": 0xA1,
    "alt": 0x12, "lalt": 0xA4, "ralt": 0xA5,
    "lwin": 0x5B, "rwin": 0x5C,
    "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "space": 0x20, "backspace": 0x08,
    "delete": 0x2E, "home": 0x24, "end": 0x23, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28,
    "`": 0xC0, "~": 0xC0, "backtick": 0xC0, "grave": 0xC0,
    "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD, "\\": 0xDC, ";": 0xBA,
    "'": 0xDE, ",": 0xBC, ".": 0xBE, "/": 0xBF,
}
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
    VK[_c.lower()] = ord(_c)
for _i in range(1, 13):
    VK[f"f{_i}"] = 0x6F + _i

EXTENDED_VKS = {0x2E, 0x24, 0x23, 0x25, 0x26, 0x27, 0x28, 0x21, 0x22, 0x2D, 0xA3, 0xA5}
CTRL_VKS = {0x11, 0xA2, 0xA3}
SHIFT_VKS = {0x10, 0xA0, 0xA1}
ALT_VKS = {0x12, 0xA4, 0xA5}
WIN_VKS = {0x5B, 0x5C}
MODIFIER_VKS = CTRL_VKS | SHIFT_VKS | ALT_VKS | WIN_VKS


def vk_from_name(name):
    name = name.strip().lower()
    if name not in VK:
        raise ValueError(f"Unknown key name: {name!r}")
    return VK[name]


# ---------------------------------------------------------------- structures
class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

# ---------------------------------------------------------------- prototypes
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.IsWindow.argtypes = [wintypes.HWND]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
user32.RegisterClipboardFormatW.restype = wintypes.UINT
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


# ---------------------------------------------------------------- keyboard injection
def _key_input(vk, up=False, mark=INJECT_MARK):
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in EXTENDED_VKS:
        flags |= KEYEVENTF_EXTENDEDKEY
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFF
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=mark)
    return inp


def send_key_events(events, mark=INJECT_MARK):
    """events: list of (vk, is_up)."""
    if not events:
        return
    arr = (INPUT * len(events))(*[_key_input(vk, up, mark) for vk, up in events])
    user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


KEYEVENTF_UNICODE = 0x0004


def type_char(ch):
    """Types one character as a real key press (layout independent)."""
    data = ch.encode("utf-16-le")
    events = []
    for i in range(0, len(data), 2):
        code = int.from_bytes(data[i:i + 2], "little")
        for up in (False, True):
            inp = INPUT(type=INPUT_KEYBOARD)
            inp.u.ki = KEYBDINPUT(wVk=0, wScan=code,
                                  dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0),
                                  time=0, dwExtraInfo=INJECT_MARK)
            events.append(inp)
    arr = (INPUT * len(events))(*events)
    user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))


def press_combo(*names):
    """press_combo('ctrl', 'a') -> Ctrl down, A down, A up, Ctrl up."""
    vks = [vk_from_name(n) if isinstance(n, str) else n for n in names]
    events = [(vk, False) for vk in vks] + [(vk, True) for vk in reversed(vks)]
    send_key_events(events)


def is_key_down(vk):
    return bool(user32.GetAsyncKeyState(vk) & 0x8000)


def wait_keys_released(vks, timeout=4.0):
    """Wait until the user physically lets go of the given keys."""
    end = time.time() + timeout
    while time.time() < end:
        if not any(is_key_down(vk) for vk in vks):
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------- clipboard
_FMT_HISTORY = user32.RegisterClipboardFormatW("CanIncludeInClipboardHistory")
_FMT_CLOUD = user32.RegisterClipboardFormatW("CanUploadToCloudClipboard")


def _open_clipboard(timeout=1.5):
    # No owner window on purpose: if we owned the clipboard through a window on
    # a thread that doesn't pump messages, the next app that copies (VS Code's
    # Ctrl+C) would hang waiting for WM_DESTROYCLIPBOARD to be answered.
    end = time.time() + timeout
    while time.time() < end:
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def _global_bytes(data):
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    return h


def get_clipboard_text():
    if not _open_clipboard():
        return None
    try:
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return None
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = kernel32.GlobalLock(h)
        try:
            return ctypes.wstring_at(p) if p else None
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text, private=True):
    """Put text on the clipboard. private=True keeps it out of Win+V history."""
    if not _open_clipboard():
        raise RuntimeError("Could not open the clipboard")
    try:
        user32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        if not user32.SetClipboardData(CF_UNICODETEXT, _global_bytes(data)):
            raise ctypes.WinError(ctypes.get_last_error())
        if private:
            zero = (0).to_bytes(4, "little")
            for fmt in (_FMT_HISTORY, _FMT_CLOUD):
                if fmt:
                    user32.SetClipboardData(fmt, _global_bytes(zero))
    finally:
        user32.CloseClipboard()


def clipboard_seq():
    return user32.GetClipboardSequenceNumber()


# ---------------------------------------------------------------- windows
def foreground_window():
    return user32.GetForegroundWindow()


def window_title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_process_name(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def is_window(hwnd):
    return bool(user32.IsWindow(hwnd))


# ---------------------------------------------------------------- hooks
class HookThread(threading.Thread):
    """Runs global low-level keyboard + mouse hooks with their own message loop.

    key_handler(vk, is_down, injected, extra) -> True to swallow the event.
    mouse_handler(msg) -> called for real mouse button presses (never swallowed).
    Handlers must return fast (Windows drops slow hooks).
    """

    def __init__(self, key_handler, mouse_handler=None):
        super().__init__(name="hooks", daemon=True)
        self.key_handler = key_handler
        self.mouse_handler = mouse_handler
        self.thread_id = None
        self.ready = threading.Event()
        self.error = None
        # keep references so the callbacks are not garbage collected
        self._kb_proc = HOOKPROC(self._kb)
        self._ms_proc = HOOKPROC(self._ms)

    def _kb(self, n_code, w_param, l_param):
        if n_code == 0:
            try:
                k = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                is_down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                injected = bool(k.flags & LLKHF_INJECTED)
                if self.key_handler(k.vkCode, is_down, injected, k.dwExtraInfo):
                    return 1
            except Exception:  # never let an exception break the keyboard
                pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def _ms(self, n_code, w_param, l_param):
        if n_code == 0 and self.mouse_handler and w_param in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN):
            try:
                m = ctypes.cast(l_param, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                if not (m.flags & LLMHF_INJECTED):
                    self.mouse_handler(w_param)
            except Exception:
                pass
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    def run(self):
        self.thread_id = kernel32.GetCurrentThreadId()
        hmod = kernel32.GetModuleHandleW(None)
        kb = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, hmod, 0)
        ms = user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, hmod, 0)
        if not kb:
            self.error = ctypes.WinError(ctypes.get_last_error())
            self.ready.set()
            return
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass
        user32.UnhookWindowsHookEx(kb)
        if ms:
            user32.UnhookWindowsHookEx(ms)

    def stop(self):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


# ---------------------------------------------------------------- misc
def set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def single_instance(name="Local\\AI_Tutor_Typing_Assistant"):
    """Returns a handle if we are the only instance, else None."""
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    h = kernel32.CreateMutexW(None, False, name)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return h


def work_area():
    """(left, top, right, bottom) of the primary monitor minus the taskbar."""
    rect = wintypes.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
    return rect.left, rect.top, rect.right, rect.bottom


def get_total_ram_gb():
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(st)
    kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    return st.ullTotalPhys / (1024 ** 3), st.ullAvailPhys / (1024 ** 3)
