import json
import os
import queue
import sys
import subprocess
import threading
import logging
from logging.handlers import RotatingFileHandler
import traceback
import atexit
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
    restore_latest,
    find_wsl_configs,
    get_wsl_host_ip,
)
from server import create_app, set_api_key

try:
    from _version import __version__
except ImportError:
    __version__ = "dev"

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
        self.root.geometry("720x580")

        self._set_window_icon()

        self.server_thread: threading.Thread | None = None
        self.server_instance: uvicorn.Server | None = None
        self.server_running = False
        self._config_backup_path: Path | None = None

        self.port_var = tk.IntVar(value=DEFAULT_PORT)
        self.api_key_var = tk.StringVar(value="")
        self._check_updates = True
        self._load_settings()

        self.config_path = get_config_path()

        self._wsl_configs: list[dict] = []
        self._wsl_backups: dict[Path, Path | None] = {}
        self._wsl_enabled = False
        self._windows_enabled = False

        self._build_ui()
        self._scan_wsl()
        self._poll_logs()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        atexit.register(self._cleanup_on_exit)
        self.root.after(500, self._check_update)

    def _set_window_icon(self):
        try:
            if getattr(sys, "frozen", False):
                if os.name == "nt":
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

        settings_frame.columnconfigure(3, weight=1)

        # Row 1 — 服务器控制
        ctrl_frame = ttk.Frame(self.root)
        ctrl_frame.pack(fill=tk.X, **pad)

        self.server_btn = ttk.Button(ctrl_frame, text="启动服务器", command=self._toggle_server)
        self.server_btn.pack(side=tk.LEFT, **pad)

        self.status_var = tk.StringVar(value="●  已停止")
        self.status_label = ttk.Label(ctrl_frame, textvariable=self.status_var, foreground="gray")
        self.status_label.pack(side=tk.LEFT, **pad)

        self.url_var = tk.StringVar(value="")
        url_label = ttk.Label(ctrl_frame, textvariable=self.url_var, foreground="blue", cursor="hand2")
        url_label.pack(side=tk.RIGHT, **pad)

        # Row 2 — Windows 代理
        win_frame = ttk.Frame(self.root)
        win_frame.pack(fill=tk.X, **pad)

        ttk.Separator(win_frame, orient="horizontal").pack(fill=tk.X, pady=(0, 4))

        self.win_toggle_btn = ttk.Button(
            win_frame, text="启用Windows代理", command=self._toggle_windows,
            state=tk.DISABLED, width=16)
        self.win_toggle_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.win_status_var = tk.StringVar(value="未启用")
        self.win_status_label = ttk.Label(
            win_frame, textvariable=self.win_status_var, foreground="gray")
        self.win_status_label.pack(side=tk.LEFT, padx=(0, 6))

        self.view_win_config_btn = ttk.Button(
            win_frame, text="查看配置", command=self._view_config,
            width=10)
        self.view_win_config_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.config_path_var = tk.StringVar(value=str(self.config_path))
        ttk.Label(win_frame, textvariable=self.config_path_var, foreground="gray").pack(
            side=tk.LEFT, fill=tk.X, expand=True)

        # Row 3 — WSL 代理
        wsl_frame = ttk.Frame(self.root)
        wsl_frame.pack(fill=tk.X, **pad)

        ttk.Separator(wsl_frame, orient="horizontal").pack(fill=tk.X, pady=(0, 4))

        self.wsl_toggle_btn = ttk.Button(
            wsl_frame, text="启用WSL代理", command=self._toggle_wsl,
            state=tk.DISABLED, width=16)
        self.wsl_toggle_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.wsl_status_var = tk.StringVar(value="未启用")
        self.wsl_status_label = ttk.Label(
            wsl_frame, textvariable=self.wsl_status_var, foreground="gray")
        self.wsl_status_label.pack(side=tk.LEFT, padx=(0, 6))

        self.view_wsl_config_btn = ttk.Button(
            wsl_frame, text="查看配置", command=self._view_wsl_configs,
            width=10)
        self.view_wsl_config_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.wsl_path_var = tk.StringVar(value="")
        ttk.Label(wsl_frame, textvariable=self.wsl_path_var, foreground="gray").pack(
            side=tk.LEFT, fill=tk.X, expand=True)

        # Row 4 — 日志
        log_header = ttk.Frame(self.root)
        log_header.pack(fill=tk.X, padx=10, pady=(4, 0))
        ttk.Label(log_header, text="日志", font=("", 9, "bold")).pack(side=tk.LEFT)
        self.log_path_var = tk.StringVar(value=f"({LOG_FILE})")
        ttk.Label(log_header, textvariable=self.log_path_var, foreground="gray",
                  font=("", 8)).pack(side=tk.LEFT, padx=6)
        self.update_check_var = tk.BooleanVar(value=self._check_updates)
        self.update_check_cb = ttk.Checkbutton(
            log_header, text="检查更新", variable=self.update_check_var,
            command=self._toggle_update_check)
        self.update_check_cb.pack(side=tk.RIGHT, padx=(0, 6))
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
    # Windows proxy toggle (local config)

    def _windows_config_apply_internal(self):
        """Backup and apply proxy config for local Windows Codex."""
        port = self.port_var.get()
        try:
            self._config_backup_path = backup(self.config_path)
        except Exception as e:
            logger.error(f"Windows config backup failed: {e}\n{traceback.format_exc()}")
            self._config_backup_path = None
            return
        if self._config_backup_path:
            logger.info(f"Windows config backed up to: {self._config_backup_path}")
        apply_codex_config(port, self.config_path)
        self._windows_enabled = True
        logger.info(f"Windows Codex config set to http://localhost:{port}/v1")
        self._refresh_config_status()

    def _windows_config_restore_internal(self):
        """Restore Windows config from backup."""
        if not self._windows_enabled:
            return
        if self._config_backup_path and self._config_backup_path.exists():
            try:
                restore(self._config_backup_path, self.config_path)
                logger.info(f"Windows config restored from: {self._config_backup_path}")
            except Exception as e:
                logger.error(f"Windows config restore failed: {e}\n{traceback.format_exc()}")
        else:
            fallback = restore_latest(self.config_path)
            if fallback:
                logger.info(f"Windows config restored from latest backup: {fallback}")
            else:
                logger.warning("No backup found for Windows config restore")
        self._config_backup_path = None
        self._windows_enabled = False
        self._refresh_config_status()

    def _update_windows_toggle_state(self):
        """Update Windows toggle button text and enabled state."""
        if not self.server_running:
            self.win_toggle_btn.configure(state="disabled")
            self._set_windows_toggle_off()
            return
        self.win_toggle_btn.configure(state="normal")
        if self._windows_enabled:
            self.win_toggle_btn.configure(text="关闭Windows代理")
            self._set_proxy_status(self.win_status_var, self.win_status_label, True)
        else:
            self.win_toggle_btn.configure(text="启用Windows代理")
            self._set_proxy_status(self.win_status_var, self.win_status_label, False)

    def _set_windows_toggle_off(self):
        self._windows_enabled = False
        self.win_toggle_btn.configure(text="启用Windows代理")
        self._set_proxy_status(self.win_status_var, self.win_status_label, False)

    def _set_proxy_status(self, status_var: tk.StringVar, status_label: ttk.Label, enabled: bool):
        status_var.set("已启用" if enabled else "未启用")
        status_label.configure(foreground="green" if enabled else "gray")

    def _toggle_windows(self):
        if self._windows_enabled:
            self._windows_config_restore_internal()
        else:
            self._windows_config_apply_internal()
        self._update_windows_toggle_state()

    def _load_settings(self):
        """Load persisted API key and port from settings.json."""
        try:
            if SETTINGS_FILE.exists():
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if data.get("api_key"):
                    self.api_key_var.set(data["api_key"])
                if data.get("port"):
                    self.port_var.set(data["port"])
                if "check_updates" in data:
                    self._check_updates = data["check_updates"]
        except Exception:
            pass

    def _save_settings(self):
        """Persist API key and port to settings.json."""
        try:
            SETTINGS_FILE.write_text(
                json.dumps({"api_key": self.api_key_var.get(), "port": self.port_var.get(), "check_updates": self._check_updates}, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存设置失败: {e}")

    def _toggle_server(self):
        if self.server_running:
            self._stop_server()
        else:
            self._start_server()

    def _start_server(self):
        port = self.port_var.get()
        api_key = self.api_key_var.get().strip()

        if not api_key:
            messagebox.showerror("错误", "请输入 DeepSeek API 密钥。")
            return

        set_api_key(api_key)

        self._save_settings()

        self.server_btn.configure(text="停止服务器")
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
        

        self._update_windows_toggle_state()
        self._update_wsl_toggle_state()

        logger.info(f"服务器在端口 {port} 启动中…")

    def _run_server(self, port: int):
        try:
            app = create_app()
            config = uvicorn.Config(
                app,
                host="0.0.0.0",
                port=port,
                log_level="info",
                log_config=None,
            )
            self.server_instance = uvicorn.Server(config)
            self.server_instance.run()
        except Exception:
            logger.error(f"服务器崩溃:\n{traceback.format_exc()}")

    def _stop_server(self):
        if self.server_instance is not None:
            self.server_instance.should_exit = True
            logger.info("服务器停止中…")

        if self.server_thread and self.server_thread.is_alive():
            self.server_thread.join(timeout=3)

        self.server_running = False
        self.server_instance = None
        self.server_thread = None

        self._windows_config_restore_internal()
        self._wsl_config_restore_internal()

        self.status_var.set("●  已停止")
        self.status_label.configure(foreground="gray")
        self.url_var.set("")
        self.server_btn.configure(text="启动服务器")
        self.port_entry.configure(state=tk.NORMAL)
        self.key_entry.configure(state=tk.NORMAL)

        self._update_windows_toggle_state()
        self._update_wsl_toggle_state()

        logger.info("服务器已停止。")

    # Codex config status

    def _refresh_config_status(self):
        self.config_path_var.set(str(self.config_path))

    def _view_config(self):
        """Show Windows Codex config content."""
        try:
            if self.config_path.exists():
                content = read_config(self.config_path)
            else:
                content = "配置文件不存在"
            self._show_config_viewer("Windows 配置", str(self.config_path), content)
        except Exception as e:
            messagebox.showerror("错误", f"无法读取配置文件:\n{e}")

    def _view_wsl_configs(self):
        """Show all WSL config contents."""
        parts = []
        for entry in self._wsl_configs:
            p = entry["config_path"]
            try:
                if p.exists():
                    content = read_config(p)
                else:
                    content = "配置文件不存在"
                parts.append(f"── {entry['distro']} ({p}) ──\n{content}")
            except Exception as e:
                parts.append(f"── {entry['distro']} ({p}) ──\n读取失败: {e}")
        if parts:
            self._show_config_viewer("WSL 配置", f"{len(parts)} 个 WSL 配置", "\n\n".join(parts))
        else:
            self._show_config_viewer("WSL 配置", "未检测到配置", "未检测到 WSL Codex 配置")

    def _show_config_viewer(self, title: str, subtitle: str, content: str):
        viewer = tk.Toplevel(self.root)
        viewer.title(title)
        viewer.geometry("760x520")
        viewer.minsize(560, 360)
        viewer.transient(self.root)

        header = ttk.Frame(viewer)
        header.pack(fill=tk.X, padx=12, pady=(12, 6))

        ttk.Label(header, text=title, font=("", 10, "bold")).pack(anchor=tk.W)
        ttk.Label(header, text=subtitle, foreground="gray").pack(anchor=tk.W, pady=(2, 0))

        text = scrolledtext.ScrolledText(
            viewer,
            wrap=tk.NONE,
            state=tk.NORMAL,
            font=("Consolas", 10),
        )
        text.pack(fill=tk.BOTH, expand=True, padx=12, pady=6)
        text.insert("1.0", content)
        text.configure(state=tk.DISABLED)

        footer = ttk.Frame(viewer)
        footer.pack(fill=tk.X, padx=12, pady=(6, 12))
        ttk.Button(footer, text="关闭", command=viewer.destroy, width=10).pack(side=tk.RIGHT)
        viewer.focus_set()

    # ── WSL config management ────────────────────────────────

    def _scan_wsl(self):
        """Detect WSL Codex configs and update UI."""
        self._wsl_configs = find_wsl_configs()
        if self._wsl_configs:
            paths = [str(c["config_path"]) for c in self._wsl_configs]
            self.wsl_path_var.set(" | ".join(paths))
        else:
            self._set_wsl_toggle_off()
            self.wsl_path_var.set("未检测到 WSL Codex 配置")
        self._update_wsl_toggle_state()

    def _update_wsl_toggle_state(self):
        """Update WSL toggle button text and enabled state."""
        if not self._wsl_configs:
            self.wsl_toggle_btn.configure(state=tk.DISABLED)
            self._set_wsl_toggle_off()
            return
        if not self.server_running:
            self.wsl_toggle_btn.configure(state=tk.DISABLED)
            self._set_wsl_toggle_off()
            return
        self.wsl_toggle_btn.configure(state=tk.NORMAL)
        if self._wsl_enabled:
            self.wsl_toggle_btn.configure(text="关闭WSL代理")
            self._set_proxy_status(self.wsl_status_var, self.wsl_status_label, True)
        else:
            self.wsl_toggle_btn.configure(text="启用WSL代理")
            self._set_proxy_status(self.wsl_status_var, self.wsl_status_label, False)

    def _set_wsl_toggle_off(self):
        self._wsl_enabled = False
        self.wsl_toggle_btn.configure(text="启用WSL代理")
        self._set_proxy_status(self.wsl_status_var, self.wsl_status_label, False)

    def _toggle_wsl(self):
        if self._wsl_enabled:
            self._wsl_config_restore_internal()
        else:
            self._wsl_config_apply_internal()
        self._update_wsl_toggle_state()

    def _wsl_config_apply_internal(self):
        """Backup and apply proxy config to all WSL configs (silent)."""
        if not self._wsl_configs:
            return
        port = self.port_var.get()
        self._wsl_backups.clear()
        for entry in self._wsl_configs:
            p = entry["config_path"]
            distro = entry["distro"]
            host = get_wsl_host_ip(distro) or "localhost"
            try:
                bk = backup(p)
                self._wsl_backups[p] = bk
                apply_codex_config(port, p, host)
                logger.info(
                    f"WSL {entry['distro']} 配置已生效"
                    f" -> http://{host}:{port}/v1"
                )
            except Exception as e:
                logger.error(
                    f"WSL {entry['distro']} 配置应用失败: {e}"
                )
        self._wsl_enabled = True

    def _wsl_config_restore_internal(self):
        """Restore all WSL configs from backups (silent)."""
        if not self._wsl_backups and not self._wsl_configs:
            return
        for entry in self._wsl_configs:
            p = entry["config_path"]
            bk = self._wsl_backups.get(p)
            try:
                if bk and bk.exists():
                    restore(bk, p)
                    logger.info(f"WSL {entry['distro']} 配置已还原")
                else:
                    fallback = restore_latest(p)
                    if fallback:
                        logger.info(
                            f"WSL {entry['distro']} 配置已从最新备份还原"
                        )
            except Exception as e:
                logger.error(
                    f"WSL {entry['distro']} 配置还原失败: {e}"
                )
        self._wsl_backups.clear()
        self._wsl_enabled = False

    # ── Cleanup ──────────────────────────────────────────────

    def _on_close(self):
        if self.server_running:
            if not messagebox.askyesno("退出确认", "服务器正在运行，确定要停止并退出吗？"):
                return
            self._stop_server()
        atexit.unregister(self._cleanup_on_exit)
        self.root.destroy()

    def _cleanup_on_exit(self):
        """Last-resort cleanup when process is killed."""
        if self._wsl_enabled:
            self._wsl_config_restore_internal()
        if self._windows_enabled:
            self._windows_config_restore_internal()


    # ── Update check ─────────────────────────────────────────

    def _toggle_update_check(self):
        self._check_updates = self.update_check_var.get()
        self._save_settings()

    def _check_update(self):
        """Check GitHub Releases for a newer version in background."""
        if not self._check_updates:
            return
        threading.Thread(target=self._check_update_thread, daemon=True).start()

    def _check_update_thread(self):
        try:
            import urllib.request
            import ssl
            url = "https://api.github.com/repos/fadeawaylove/proxy-to-codex/releases/latest"
            ctx = ssl.create_default_context()
            req = urllib.request.Request(
                url,
                headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "proxy-to-codex"},
            )
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                data = json.loads(resp.read())
            latest = data.get("tag_name", "").lstrip("v")
            if not latest:
                return
            current = __version__.lstrip("v")
            if latest == current:
                logger.debug(f"Already latest version ({current})")
                return
            latest_parts = tuple(int(x) for x in latest.split(".") if x.isdigit())
            current_parts = tuple(int(x) for x in current.split(".") if x.isdigit())
            if latest_parts > current_parts:
                logger.info(f"New version available: v{latest} (current: v{current})")
                self.root.after(0, lambda: self._show_update_notification(latest))
        except Exception:
            pass

    def _show_update_notification(self, latest: str):
        release_url = f"https://github.com/fadeawaylove/proxy-to-codex/releases/tag/v{latest}"
        sep = "\u2500" * 60
        log_msg = (
            f"{sep}\n"
            f"   New version available: v{latest}\n"
            f"   Download: {release_url}\n"
            f"{sep}"
        )
        self.log_widget.configure(state=tk.NORMAL)
        self.log_widget.insert("1.0", log_msg + "\n", "WARNING")
        self.log_widget.configure(state=tk.DISABLED)
        messagebox.showinfo(
            "\u53d1\u73b0\u65b0\u7248\u672c",
            f"\u65b0\u7248\u672c v{latest} \u5df2\u53d1\u5e03\uff01\n\n\u4e0b\u8f7d\u5730\u5740\uff1a\n{release_url}"
        )
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
        tb = traceback.format_exc()
        logger.error(f"GUI 启动失败:\n{tb}")
        messagebox.showerror("致命错误", f"GUI 启动失败:\n{tb}")
        sys.exit(1)

    root.mainloop()

if __name__ == "__main__":
    main()
