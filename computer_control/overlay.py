"""Optional on-screen stop indicator (a small always-on-top banner).

Shows "SAFETY STOP ACTIVE" with the configured hotkey while the emergency
stop is engaged, so the human at the machine sees the state without hunting
for it. Runs entirely in a background thread; degrades to a no-op when
tkinter is unavailable (headless sessions, minimal installs).
"""

from __future__ import annotations

import queue
import threading

_TEXT_TEMPLATE = "SAFETY STOP ACTIVE - press %s to resume"


class StopIndicator:
    """Thread-safe wrapper around a tkinter banner window."""

    def __init__(self, hotkey: str = ""):
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._thread = None
        self._ok = False
        self._hotkey = hotkey or ""
        try:
            import importlib.util

            if importlib.util.find_spec("tkinter") is None:
                self._ok = False
                return
            thread = threading.Thread(target=self._run, name="stop-indicator", daemon=True)
            self._thread = thread
            thread.start()
        except Exception:
            self._ok = False

    def _run(self) -> None:
        try:
            import tkinter as tk

            root = tk.Tk()
        except Exception:
            self._ok = False
            return
        self._ok = True
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        try:
            root.attributes("-alpha", 0.92)
        except Exception:
            pass
        label = tk.Label(root, text=_TEXT_TEMPLATE % self._hotkey,
                         bg="#b03030", fg="white", font=("Segoe UI", 11, "bold"),
                         padx=14, pady=8)
        label.pack()
        root.withdraw()
        self._root = root
        root.after(60, self._poll)
        root.mainloop()

    def _poll(self) -> None:
        try:
            while True:
                message = self._queue.get_nowait()
                if message == "show":
                    self._place()
                elif message == "hide":
                    self._root.withdraw()
        except queue.Empty:
            pass
        if self._root is not None:
            try:
                self._root.after(60, self._poll)
            except Exception:
                pass

    def _place(self) -> None:
        root = self._root
        root.update_idletasks()
        width = root.winfo_reqwidth()
        height = root.winfo_reqheight()
        screen_w = root.winfo_screenwidth()
        screen_h = root.winfo_screenheight()
        root.geometry("%dx%d+%d+%d" % (width, height, screen_w - width - 16, screen_h - height - 64))
        root.deiconify()
        root.lift()

    def show(self) -> None:
        if not self._ok:
            return
        self._queue.put("show")

    def hide(self) -> None:
        if not self._ok:
            return
        self._queue.put("hide")

    def close(self) -> None:
        if not self._ok:
            return
        try:
            self._root.destroy()
        except Exception:
            pass
