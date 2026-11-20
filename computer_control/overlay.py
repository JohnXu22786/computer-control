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
        self._command_lock = threading.Lock()
        self._accepting_commands = False
        self._hotkey = hotkey or ""
        try:
            import importlib.util

            if importlib.util.find_spec("tkinter") is None:
                return
            thread = threading.Thread(target=self._run, name="stop-indicator", daemon=True)
            self._thread = thread
            with self._command_lock:
                self._accepting_commands = True
            thread.start()
        except Exception:
            with self._command_lock:
                self._accepting_commands = False

    def _run(self) -> None:
        try:
            import tkinter as tk

            root = tk.Tk()
        except Exception:
            with self._command_lock:
                self._accepting_commands = False
            return
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
                elif message == "close":
                    try:
                        self._root.destroy()
                    except Exception:
                        pass
                    return
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
        with self._command_lock:
            if not self._accepting_commands or self._thread is None or not self._thread.is_alive():
                return
            self._queue.put("show")

    def hide(self) -> None:
        with self._command_lock:
            if not self._accepting_commands or self._thread is None or not self._thread.is_alive():
                return
            self._queue.put("hide")

    def close(self) -> None:
        with self._command_lock:
            if not self._accepting_commands or self._thread is None or not self._thread.is_alive():
                return
            self._accepting_commands = False
            self._queue.put("close")
