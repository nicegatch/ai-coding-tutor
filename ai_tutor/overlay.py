"""
Tiny status bubble in the bottom-right corner (no dashboard).
It is click-through, never takes keyboard focus and hides itself when idle.
"""
import ctypes
import queue
import tkinter as tk

from . import winio

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000

COLORS = {
    "info": "#2b4c7e", "think": "#5b3d8f", "type": "#1f6f43", "pause": "#9a6a00",
    "error": "#9b2c2c", "ok": "#1f6f43", "teach": "#7a3f12",
}


class Overlay:
    def __init__(self, enabled=True, opacity=0.93):
        self.enabled = enabled
        self.opacity = opacity
        self.q = queue.Queue()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#202020")
        self.label = tk.Label(self.root, text="", justify="left", anchor="w", fg="white",
                              bg="#202020", font=("Segoe UI", 10), padx=14, pady=8, wraplength=460)
        self.label.pack()
        self._hide_job = None
        self._shown = False
        self.root.after(60, self._poll)

    # --------------------------------------------------------- thread-safe API
    def show(self, text, kind="info", seconds=None):
        self.q.put(("show", text, kind, seconds))

    def hide(self):
        self.q.put(("hide", None, None, None))

    def quit(self):
        self.q.put(("quit", None, None, None))

    def run(self):
        self.root.mainloop()

    # --------------------------------------------------------- tk thread only
    def _make_click_through(self):
        user32 = ctypes.windll.user32
        user32.GetParent.restype = ctypes.c_void_p
        hwnd = user32.GetParent(ctypes.c_void_p(self.root.winfo_id())) or self.root.winfo_id()
        style = user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
        style |= WS_EX_TOPMOST | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_NOACTIVATE
        user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE, style)
        self.hwnd = hwnd

    def _poll(self):
        try:
            while True:
                cmd, text, kind, seconds = self.q.get_nowait()
                if cmd == "quit":
                    self.root.destroy()
                    return
                if cmd == "hide":
                    self._hide()
                elif self.enabled:
                    self._show(text, kind, seconds)
        except queue.Empty:
            pass
        self.root.after(60, self._poll)

    def _show(self, text, kind, seconds):
        color = COLORS.get(kind, COLORS["info"])
        self.label.configure(text=text, bg=color)
        self.root.configure(bg=color)
        self.root.update_idletasks()
        w, h = self.label.winfo_reqwidth(), self.label.winfo_reqheight()
        left, top, right, bottom = winio.work_area()
        x, y = right - w - 18, bottom - h - 18
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        if not self._shown:
            previous = winio.foreground_window()
            self.root.attributes("-alpha", 0.0)
            self._make_click_through()
            self.root.deiconify()
            self._make_click_through()
            self.root.update_idletasks()
            # if Windows handed the keyboard focus to the bubble, give it straight back
            if previous and winio.foreground_window() != previous:
                ctypes.windll.user32.SetForegroundWindow(previous)
            self._shown = True
        self.root.attributes("-alpha", self.opacity)
        self.root.lift()
        if self._hide_job:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None
        if seconds:
            self._hide_job = self.root.after(int(seconds * 1000), self._hide)

    def _hide(self):
        self._hide_job = None
        if self._shown:
            self.root.attributes("-alpha", 0.0)
