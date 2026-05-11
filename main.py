import json
import os
import queue
import sys
import threading
import logging
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import ttk
import tkinter as tk
from tkinter import messagebox, scrolledtext

import uvicorn

from codex_config import (
    get_config_path,
    backup,
    list_backups,
    read_config,
    apply as apply_codex_config,
    restore_latest,
)
from server import create_app

# ── Log capture ─────────────────────────────────────────────
log_queue: queue.Queue[str] = queue.Queue()


class QueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        log_queue.put(msg)


queue_handler = QueueHandler()
queue_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s",
                                             datefmt="%H:%M:%S"))

root_logger = logging.getLogger()
root_logger.addHandler(queue_handler)
root_logger.setLevel(logging.INFO)

# Also capture uvicorn logs
for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    uvicorn_logger = logging.getLogger(name)
    uvicorn_logger.addHandler(queue_handler)
    uvicorn_logger.propagate = False

logger = logging.getLogger("proxy-to-codex.gui")


# ── GUI Application ─────────────────────────────────────────
class ProxyGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Proxy to Codex")
        self.root.resizable(True, True)
        self.root.minsize(600, 480)
        self.root.geometry("700x580")

        self.server_thread: threading.Thread | None = None
        self.server_instance: uvicorn.Server | None = None
        self.server_running = False

        self.port_var = tk.IntVar(value=int(os.environ.get("PROXY_PORT", "4000")))
        self.api_key_var = tk.StringVar(value=os.environ.get("DEEPSEEK_API_KEY", ""))

        self.config_path = get_config_path()

        self._build_ui()
        self._refresh_config_status()
        self._poll_logs()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI construction ──────────────────────────────────────

    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        # Row 0 — Server settings
        settings_frame = ttk.LabelFrame(self.root, text="Server Settings")
        settings_frame.pack(fill=tk.X, **pad)

        ttk.Label(settings_frame, text="Port:").grid(row=0, column=0, sticky=tk.W, **pad)
        self.port_entry = ttk.Entry(settings_frame, textvariable=self.port_var, width=8)
        self.port_entry.grid(row=0, column=1, sticky=tk.W, **pad)

        ttk.Label(settings_frame, text="DeepSeek API Key:").grid(row=0, column=2, sticky=tk.W, **pad)
        self.key_entry = ttk.Entry(settings_frame, textvariable=self.api_key_var, width=40, show="*")
        self.key_entry.grid(row=0, column=3, sticky=tk.EW, **pad)

        # Row 1 — Start/Stop
        ctrl_frame = ttk.Frame(self.root)
        ctrl_frame.pack(fill=tk.X, **pad)

        self.start_btn = ttk.Button(ctrl_frame, text="Start Server", command=self._start_server)
        self.start_btn.pack(side=tk.LEFT, **pad)

        self.stop_btn = ttk.Button(ctrl_frame, text="Stop Server", command=self._stop_server, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, **pad)

        self.status_var = tk.StringVar(value="●  Stopped")
        self.status_label = ttk.Label(ctrl_frame, textvariable=self.status_var, foreground="gray")
        self.status_label.pack(side=tk.LEFT, **pad)

        self.url_var = tk.StringVar(value="")
        url_label = ttk.Label(ctrl_frame, textvariable=self.url_var, foreground="blue", cursor="hand2")
        url_label.pack(side=tk.RIGHT, **pad)

        # Row 2 — Codex Config
        codex_frame = ttk.LabelFrame(self.root, text="Codex Configuration")
        codex_frame.pack(fill=tk.X, **pad)

        self.config_path_var = tk.StringVar()
        ttk.Label(codex_frame, text="Config:").grid(row=0, column=0, sticky=tk.W, **pad)
        ttk.Label(codex_frame, textvariable=self.config_path_var, foreground="gray").grid(
            row=0, column=1, columnspan=3, sticky=tk.W, **pad)

        self.config_status_var = tk.StringVar(value="(checking...)")
        ttk.Label(codex_frame, text="Status:").grid(row=1, column=0, sticky=tk.W, **pad)
        ttk.Label(codex_frame, textvariable=self.config_status_var).grid(
            row=1, column=1, columnspan=3, sticky=tk.W, **pad)

        self.backup_btn = ttk.Button(codex_frame, text="Backup Config", command=self._backup_config)
        self.backup_btn.grid(row=2, column=0, **pad)

        self.apply_btn = ttk.Button(codex_frame, text="Auto-Configure Codex", command=self._apply_config)
        self.apply_btn.grid(row=2, column=1, **pad)

        self.restore_btn = ttk.Button(codex_frame, text="Restore Backup", command=self._restore_config)
        self.restore_btn.grid(row=2, column=2, **pad)

        # Row 3 — Log output
        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)

        self.log_widget = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, state=tk.DISABLED,
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white",
        )
        self.log_widget.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        # Configure text tags for different log levels
        self.log_widget.tag_config("ERROR", foreground="#f44747")
        self.log_widget.tag_config("WARNING", foreground="#e5c07b")
        self.log_widget.tag_config("INFO", foreground="#d4d4d4")
        self.log_widget.tag_config("DEBUG", foreground="#808080")

        settings_frame.columnconfigure(3, weight=1)
        codex_frame.columnconfigure(1, weight=1)

    # ── Log polling ──────────────────────────────────────────

    def _poll_logs(self):
        """Poll the log queue and insert messages into the log widget."""
        while True:
            try:
                msg = log_queue.get_nowait()
            except queue.Empty:
                break

            if "ERROR" in msg or "error" in msg:
                tag = "ERROR"
            elif "WARNING" in msg:
                tag = "WARNING"
            else:
                tag = "INFO"

            self.log_widget.configure(state=tk.NORMAL)
            self.log_widget.insert(tk.END, msg + "\n", tag)
            self.log_widget.see(tk.END)
            self.log_widget.configure(state=tk.DISABLED)

        self.root.after(100, self._poll_logs)

    # ── Server control ───────────────────────────────────────

    def _start_server(self):
        port = self.port_var.get()
        api_key = self.api_key_var.get().strip()

        if not api_key:
            messagebox.showerror("Error", "DeepSeek API Key is required.")
            return

        os.environ["DEEPSEEK_API_KEY"] = api_key

        self.start_btn.configure(state=tk.DISABLED)
        self.port_entry.configure(state=tk.DISABLED)
        self.key_entry.configure(state=tk.DISABLED)

        self.server_thread = threading.Thread(
            target=self._run_server, args=(port,), daemon=True
        )
        self.server_thread.start()
        self.server_running = True

        self.status_var.set("●  Running")
        self.status_label.configure(foreground="green")
        self.url_var.set(f"http://localhost:{port}")
        self.stop_btn.configure(state=tk.NORMAL)

        logger.info(f"Server starting on port {port}...")

    def _run_server(self, port: int):
        try:
            app = create_app()
            config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
            self.server_instance = uvicorn.Server(config)
            self.server_instance.run()
        except Exception:
            logger.error(f"Server crashed:\n{traceback.format_exc()}")

    def _stop_server(self):
        if self.server_instance:
            self.server_instance.should_exit = True
            logger.info("Server stopping...")

        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=3)

        self.server_running = False
        self.server_instance = None
        self.server_thread = None

        self.status_var.set("●  Stopped")
        self.status_label.configure(foreground="gray")
        self.url_var.set("")
        self.stop_btn.configure(state=tk.DISABLED)
        self.start_btn.configure(state=tk.NORMAL)
        self.port_entry.configure(state=tk.NORMAL)
        self.key_entry.configure(state=tk.NORMAL)

        logger.info("Server stopped.")

    # ── Codex config management ──────────────────────────────

    def _refresh_config_status(self):
        self.config_path_var.set(str(self.config_path))
        if self.config_path.exists():
            content = read_config(self.config_path)
            if "localhost" in content:
                self.config_status_var.set("Configured for proxy")
            else:
                self.config_status_var.set("Default (OpenAI API)")
            self.backup_btn.configure(state=tk.NORMAL)
            self.apply_btn.configure(state=tk.NORMAL)
        else:
            self.config_status_var.set("No config file found")
            self.backup_btn.configure(state=tk.DISABLED)

        backups = list_backups(self.config_path)
        self.restore_btn.configure(
            state=tk.NORMAL if backups else tk.DISABLED
        )

    def _backup_config(self):
        result = backup(self.config_path)
        if result:
            messagebox.showinfo("Backup", f"Config backed up to:\n{result}")
            logger.info(f"Config backed up to: {result}")
        else:
            messagebox.showwarning("Backup", "No config file found to backup.")
        self._refresh_config_status()

    def _apply_config(self):
        port = self.port_var.get()
        # Auto-backup before applying
        backup(self.config_path)
        apply_codex_config(port, self.config_path)
        logger.info(f"Codex config set to http://localhost:{port}/v1")
        messagebox.showinfo(
            "Configured",
            f"Codex config updated.\nProxy URL: http://localhost:{port}/v1\n\nOld config was backed up automatically.",
        )
        self._refresh_config_status()

    def _restore_config(self):
        result = restore_latest(self.config_path)
        if result:
            logger.info(f"Config restored from: {result}")
            messagebox.showinfo("Restored", f"Config restored from:\n{result}")
        else:
            messagebox.showwarning("Restore", "No backup found.")
        self._refresh_config_status()

    # ── Cleanup ──────────────────────────────────────────────

    def _on_close(self):
        if self.server_running:
            if messagebox.askyesno("Exit", "Server is running. Stop and exit?"):
                self._stop_server()
                self.root.destroy()
        else:
            self.root.destroy()


# ── Main entry ──────────────────────────────────────────────
def main():
    root = tk.Tk()

    # Set theme if available
    style = ttk.Style()
    try:
        available = style.theme_names()
        if "vista" in available:
            style.theme_use("vista")
        elif "clam" in available:
            style.theme_use("clam")
    except Exception:
        pass

    try:
        app = ProxyGUI(root)
    except Exception:
        messagebox.showerror("Fatal Error", f"Failed to start GUI:\n{traceback.format_exc()}")
        sys.exit(1)

    root.mainloop()


if __name__ == "__main__":
    main()
