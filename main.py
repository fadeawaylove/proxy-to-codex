import json
import os
import queue
import sys
import subprocess
import threading
import logging
from logging.handlers import RotatingFileHandler
import traceback
from pathlib import Path
from tkinter import ttk
import tkinter as tk
from tkinter import messagebox, scrolledtext

import uvicorn

from codex_config import (
    get_config_path,
    backup,
    read_config,
    apply as apply_codex_config,
    restore,
)
from server import create_app, set_api_key

# ── Log file path ───────────────────────────────────────────
def _log_file_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    log_dir = Path(appdata) / "proxy-to-codex" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "proxy.log"

LOG_FILE = _log_file_path()

def _settings_dir() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home()))
    d = Path(appdata) / "proxy-to-codex"
    d.mkdir(parents=True, exist_ok=True)
    return d

SETTINGS_FILE = _settings_dir() / "settings.json"

# ── File logging (DEBUG) ────────────────────────────────────
file_handler = RotatingFileHandler(
    str(LOG_FILE), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

root_logger = logging.getLogger()
root_logger.addHandler(file_handler)
root_logger.setLevel(logging.DEBUG)

# ── GUI log capture (INFO) ──────────────────────────────────
log_queue: queue.Queue[str] = queue.Queue()


class QueueHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        log_queue.put(msg)


queue_handler = QueueHandler()
queue_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s", datefmt="%H:%M:%S"))
queue_handler.setLevel(logging.INFO)

root_logger.addHandler(queue_handler)

for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
    uvicorn_logger = logging.getLogger(name)
    uvicorn_logger.addHandler(queue_handler)
    uvicorn_logger.propagate = False

logger = logging.getLogger("proxy-to-codex.gui")

# ── Default port ────────────────────────────────────────────
DEFAULT_PORT = 43214


# ── GUI Application ─────────────────────────────────────────
class ProxyGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Codex 代理管理")
        self.root.resizable(True, True)
        self.root.minsize(620, 480)
        self.root.geometry("720x560")

        self._set_window_icon()

        self.server_thread: threading.Thread | None = None
        self.server_instance: uvicorn.Server | None = None
        self.server_running = False
        self._config_backup_path: Path | None = None

        self.port_var = tk.IntVar(value=DEFAULT_PORT)
        self.api_key_var = tk.StringVar(value="")
        self._load_settings()

        self.config_path = get_config_path()

        self._build_ui()
        self._refresh_config_status()
        self._poll_logs()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_window_icon(self):
        try:
            if getattr(sys, "frozen", False):
                if os.name == "nt":
                    # Use the icon resource embedded in the exe by --icon
                    self.root.iconbitmap(sys.executable)
            else:
                icon_dir = Path(__file__).parent
                ico_path = icon_dir / "icon.ico"
                png_path = icon_dir / "icon.png"
                if os.name == "nt" and ico_path.exists():
                    self.root.iconbitmap(str(ico_path))
                elif png_path.exists():
                    self.root.iconphoto(True, tk.PhotoImage(file=str(png_path)))
        except Exception:
            pass

    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}

        # Row 0 — 服务器设置
        settings_frame = ttk.LabelFrame(self.root, text="服务器设置")
        settings_frame.pack(fill=tk.X, **pad)

        ttk.Label(settings_frame, text="端口:").grid(row=0, column=0, sticky=tk.W, **pad)
        self.port_entry = ttk.Entry(settings_frame, textvariable=self.port_var, width=8)
        self.port_entry.grid(row=0, column=1, sticky=tk.W, **pad)

        ttk.Label(settings_frame, text="DeepSeek API 密钥:").grid(row=0, column=2, sticky=tk.W, **pad)
        self.key_entry = ttk.Entry(settings_frame, textvariable=self.api_key_var, width=40, show="*")
        self.key_entry.grid(row=0, column=3, sticky=tk.EW, **pad)

        # Row 1 — Start/Stop
        ctrl_frame = ttk.Frame(self.root)
        ctrl_frame.pack(fill=tk.X, **pad)

        self.start_btn = ttk.Button(ctrl_frame, text="启动服务器", command=self._start_server)
        self.start_btn.pack(side=tk.LEFT, **pad)

        self.stop_btn = ttk.Button(ctrl_frame, text="停止服务器", command=self._stop_server, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, **pad)

        self.status_var = tk.StringVar(value="●  已停止")
        self.status_label = ttk.Label(ctrl_frame, textvariable=self.status_var, foreground="gray")
        self.status_label.pack(side=tk.LEFT, **pad)

        self.url_var = tk.StringVar(value="")
        url_label = ttk.Label(ctrl_frame, textvariable=self.url_var, foreground="blue", cursor="hand2")
        url_label.pack(side=tk.RIGHT, **pad)

        # Row 2 — Codex 配置
        codex_frame = ttk.LabelFrame(self.root, text="Codex 配置")
        codex_frame.pack(fill=tk.X, **pad)

        self.config_path_var = tk.StringVar()
        ttk.Label(codex_frame, text="配置文件:").grid(row=0, column=0, sticky=tk.W, **pad)
        ttk.Label(codex_frame, textvariable=self.config_path_var, foreground="gray").grid(
            row=0, column=1, columnspan=2, sticky=tk.W, **pad)

        self.config_status_var = tk.StringVar(value="(检查中…)")
        ttk.Label(codex_frame, text="状态:").grid(row=1, column=0, sticky=tk.W, **pad)
        ttk.Label(codex_frame, textvariable=self.config_status_var).grid(
            row=1, column=1, columnspan=2, sticky=tk.W, **pad)

        self.view_config_btn = ttk.Button(codex_frame, text="查看配置", command=self._view_config)
        self.view_config_btn.grid(row=2, column=0, **pad)

        # Row 3 — 日志
        log_header = ttk.Frame(self.root)
        log_header.pack(fill=tk.X, padx=10, pady=(4, 0))
        ttk.Label(log_header, text="日志", font=("", 9, "bold")).pack(side=tk.LEFT)
        self.log_path_var = tk.StringVar(value=f"({LOG_FILE})")
        ttk.Label(log_header, textvariable=self.log_path_var, foreground="gray",
                  font=("", 8)).pack(side=tk.LEFT, padx=6)
        ttk.Button(log_header, text="清空日志", command=self._clear_log).pack(side=tk.RIGHT)

        log_frame = ttk.Frame(self.root)
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)

        self.log_widget = scrolledtext.ScrolledText(
            log_frame, wrap=tk.WORD, state=tk.DISABLED,
            font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
            insertbackground="white",
        )
        self.log_widget.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        self.log_widget.tag_config("ERROR", foreground="#f44747")
        self.log_widget.tag_config("WARNING", foreground="#e5c07b")
        self.log_widget.tag_config("INFO", foreground="#d4d4d4")
        self.log_widget.tag_config("DEBUG", foreground="#808080")

        settings_frame.columnconfigure(3, weight=1)
        codex_frame.columnconfigure(1, weight=1)

    # ── Log polling ──────────────────────────────────────────

    def _poll_logs(self):
        while True:
            try:
                msg = log_queue.get_nowait()
            except queue.Empty:
                break

            if " ERROR " in msg or "ERROR" in msg.split("  ")[:2]:
                tag = "ERROR"
            elif " WARNING " in msg:
                tag = "WARNING"
            else:
                tag = "INFO"

            self.log_widget.configure(state=tk.NORMAL)
            self.log_widget.insert(tk.END, msg + "\n", tag)
            self.log_widget.see(tk.END)
            self.log_widget.configure(state=tk.DISABLED)

        self.root.after(100, self._poll_logs)

    # ── Server control ───────────────────────────────────────

    def _auto_config_apply(self):
        """Backup current config, then apply proxy config. Track backup for later restore."""
        try:
            self._config_backup_path = backup(self.config_path)
        except Exception as e:
            logger.error(f"配置备份异常: {e}\n{traceback.format_exc()}")
            self._config_backup_path = None
        if self._config_backup_path:
            logger.info(f"配置已自动备份到: {self._config_backup_path}")
        port = self.port_var.get()
        apply_codex_config(port, self.config_path)
        logger.info(f"Codex 配置已自动设为 http://localhost:{port}/v1")
        self._refresh_config_status()

    def _auto_config_restore(self):
        """Restore config from the backup created on start, if any."""
        if not self._config_backup_path:
            return
        if not self._config_backup_path.exists():
            logger.warning(f"备份文件已不存在: {self._config_backup_path}")
            self._config_backup_path = None
            return
        try:
            success = restore(self._config_backup_path, self.config_path)
            if success:
                logger.info(f"配置已自动还原自: {self._config_backup_path}")
            else:
                logger.warning(f"配置还原失败: {self._config_backup_path}")
        except Exception as e:
            logger.error(f"配置还原异常: {e}\n{traceback.format_exc()}")
        finally:
            self._config_backup_path = None
            self._refresh_config_status()

    def _load_settings(self):
        """Load persisted API key and port from settings.json."""
        try:
            if SETTINGS_FILE.exists():
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if data.get("api_key"):
                    self.api_key_var.set(data["api_key"])
                if data.get("port"):
                    self.port_var.set(data["port"])
        except Exception:
            pass

    def _save_settings(self):
        """Persist API key and port to settings.json."""
        try:
            SETTINGS_FILE.write_text(
                json.dumps({"api_key": self.api_key_var.get(), "port": self.port_var.get()}, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存设置失败: {e}")

    def _start_server(self):
        port = self.port_var.get()
        api_key = self.api_key_var.get().strip()

        if not api_key:
            messagebox.showerror("错误", "请输入 DeepSeek API 密钥。")
            return

        set_api_key(api_key)

        self._save_settings()

        self._auto_config_apply()

        self.start_btn.configure(state=tk.DISABLED)
        self.port_entry.configure(state=tk.DISABLED)
        self.key_entry.configure(state=tk.DISABLED)

        self.server_thread = threading.Thread(
            target=self._run_server, args=(port,), daemon=True
        )
        self.server_thread.start()
        self.server_running = True

        self.status_var.set("●  运行中")
        self.status_label.configure(foreground="green")
        self.url_var.set(f"http://localhost:{port}")
        self.stop_btn.configure(state=tk.NORMAL)

        logger.info(f"服务器在端口 {port} 启动中…")

    def _run_server(self, port: int):
        try:
            app = create_app()
            config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
            self.server_instance = uvicorn.Server(config)
            self.server_instance.run()
        except Exception:
            logger.error(f"服务器崩溃:\n{traceback.format_exc()}")

    def _stop_server(self):
        if self.server_instance:
            self.server_instance.should_exit = True
            logger.info("服务器停止中…")

        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=3)

        self.server_running = False
        self.server_instance = None
        self.server_thread = None

        self._auto_config_restore()

        self.status_var.set("●  已停止")
        self.status_label.configure(foreground="gray")
        self.url_var.set("")
        self.stop_btn.configure(state=tk.DISABLED)
        self.start_btn.configure(state=tk.NORMAL)
        self.port_entry.configure(state=tk.NORMAL)
        self.key_entry.configure(state=tk.NORMAL)

        logger.info("服务器已停止。")

    # ── Codex config ─────────────────────────────────────────

    def _refresh_config_status(self):
        self.config_path_var.set(str(self.config_path))
        if self.config_path.exists():
            content = read_config(self.config_path)
            if "localhost" in content:
                self.config_status_var.set("已配置本地代理")
            else:
                self.config_status_var.set("默认 (OpenAI API)")
            self.view_config_btn.configure(state=tk.NORMAL)
        else:
            self.config_status_var.set("未找到配置文件")
            self.view_config_btn.configure(state=tk.DISABLED)

    def _view_config(self):
        """Open the config file with the system default editor."""
        try:
            os.startfile(str(self.config_path))
        except Exception as e:
            messagebox.showerror("错误", f"无法打开配置文件:\n{e}")

    # ── Cleanup ──────────────────────────────────────────────

    def _on_close(self):
        if self.server_running:
            if messagebox.askyesno("退出确认", "服务器正在运行，确定要停止并退出吗？"):
                self._stop_server()
                self.root.destroy()
        else:
            self.root.destroy()

    def _clear_log(self):
        self.log_widget.configure(state=tk.NORMAL)
        self.log_widget.delete("1.0", tk.END)
        self.log_widget.configure(state=tk.DISABLED)
        logger.debug("日志窗口已清空")


# ── Main entry ──────────────────────────────────────────────
def main():
    root = tk.Tk()

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
        messagebox.showerror("致命错误", f"GUI 启动失败:\n{traceback.format_exc()}")
        sys.exit(1)

    root.mainloop()


if __name__ == "__main__":
    main()
