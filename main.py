import json
import os
import queue
import sys
import subprocess
import threading
import logging
from logging.handlers import RotatingFileHandler
import traceback
import shutil
import webbrowser
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
import tomlkit
import uvicorn

from server import create_app, set_api_key, DEFAULT_MODEL_MAP, DEFAULT_BASE_URL

try:
    from _version import __version__
except ImportError:
    __version__ = "dev"

# ── Theme ────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

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
CODEX_PROFILE_DIR = _settings_dir() / "codex-profile"
CODEX_PROFILE_CONFIG = CODEX_PROFILE_DIR / "config.toml"
CODEX_PROFILE_AUTH = CODEX_PROFILE_DIR / "auth.json"

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
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.root.title("Codex 代理管理")
        self.root.resizable(True, True)
        self.root.minsize(680, 540)
        self.root.geometry("780x640")

        self._set_window_icon()

        self.server_thread: threading.Thread | None = None
        self.server_instance: uvicorn.Server | None = None
        self.server_running = False
        self._server_starting = False

        self.port_var = tk.IntVar(value=DEFAULT_PORT)
        self.api_key_var = tk.StringVar(value="")
        self.workdir_var = tk.StringVar(value=str(Path.home()))
        self.model_map: dict[str, str] = {}
        self.base_url: str = DEFAULT_BASE_URL
        self._load_settings()

        self._build_ui()
        self._poll_logs()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

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
        pad = {"padx": 12, "pady": 6}

        # ── Row 0 — 服务器设置 ──────────────────────────────
        settings_frame = ctk.CTkFrame(self.root)
        settings_frame.pack(fill=tk.X, **pad)

        ctk.CTkLabel(settings_frame, text="服务器设置", font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor=tk.W, padx=10, pady=(6, 4))

        row0 = ctk.CTkFrame(settings_frame, fg_color="transparent")
        row0.pack(fill=tk.X, padx=10, pady=(0, 6))

        ctk.CTkLabel(row0, text="端口:").pack(side=tk.LEFT, padx=(0, 4))
        self.port_entry = ctk.CTkEntry(row0, width=70, textvariable=self.port_var)
        self.port_entry.pack(side=tk.LEFT, padx=(0, 12))

        ctk.CTkLabel(row0, text="API 密钥:").pack(side=tk.LEFT, padx=(0, 4))
        self.key_entry = ctk.CTkEntry(row0, width=260, textvariable=self.api_key_var, show="*")
        self.key_entry.pack(side=tk.LEFT, padx=(0, 8))

        # ── Row 1 — 服务器控制 ──────────────────────────────
        ctrl_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        ctrl_frame.pack(fill=tk.X, **pad)

        self.server_btn = ctk.CTkButton(ctrl_frame, text="启动服务器", width=110,
                                         command=self._toggle_server)
        self.server_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.status_var = tk.StringVar(value="●  已停止")
        self.status_label = ctk.CTkLabel(ctrl_frame, textvariable=self.status_var,
                                          text_color="gray")
        self.status_label.pack(side=tk.LEFT, padx=(0, 8))

        self.model_settings_btn = ctk.CTkButton(ctrl_frame, text="模型设置", width=90,
                                                 command=self._open_model_settings)
        self.model_settings_btn.pack(side=tk.LEFT)

        self.url_var = tk.StringVar(value="")
        ctk.CTkLabel(ctrl_frame, textvariable=self.url_var, text_color="#5294e2").pack(
            side=tk.RIGHT)

        # ── Row 2 — Codex 使用方式 ─────────────────────────
        sep1 = ctk.CTkFrame(self.root, height=1, fg_color=("gray50", "gray30"))
        sep1.pack(fill=tk.X, padx=12, pady=(2, 0))

        codex_frame = ctk.CTkFrame(self.root)
        codex_frame.pack(fill=tk.X, **pad)

        ctk.CTkLabel(codex_frame, text="Codex 使用方式",
                      font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor=tk.W, padx=10, pady=(6, 4))

        workdir_row = ctk.CTkFrame(codex_frame, fg_color="transparent")
        workdir_row.pack(fill=tk.X, padx=10, pady=(0, 6))

        ctk.CTkLabel(workdir_row, text="工作目录:").pack(side=tk.LEFT, padx=(0, 4))
        self.workdir_entry = ctk.CTkEntry(workdir_row, textvariable=self.workdir_var)
        self.workdir_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))
        ctk.CTkButton(workdir_row, text="选择", width=64,
                       command=self._choose_workdir).pack(side=tk.LEFT)

        codex_btn_row = ctk.CTkFrame(codex_frame, fg_color="transparent")
        codex_btn_row.pack(fill=tk.X, padx=10, pady=(0, 8))

        self.launch_codex_btn = ctk.CTkButton(
            codex_btn_row, text="打开代理版 Codex", command=self._launch_codex,
            state="disabled", width=140)
        self.launch_codex_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.copy_cmd_btn = ctk.CTkButton(
            codex_btn_row, text="复制启动命令", command=self._copy_launch_command,
            width=120)
        self.copy_cmd_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.view_profile_btn = ctk.CTkButton(
            codex_btn_row, text="查看代理配置", command=self._view_profile_config,
            width=110)
        self.view_profile_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.profile_path_var = tk.StringVar(value=f"Profile: {CODEX_PROFILE_DIR}")
        ctk.CTkLabel(codex_btn_row, textvariable=self.profile_path_var,
                      text_color="gray").pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ── Row 3 — 日志 ────────────────────────────────────
        log_header = ctk.CTkFrame(self.root, fg_color="transparent")
        log_header.pack(fill=tk.X, padx=12, pady=(8, 0))

        ctk.CTkLabel(log_header, text="日志", font=ctk.CTkFont(size=12, weight="bold")).pack(
            side=tk.LEFT)
        self.log_path_var = tk.StringVar(value=f"({LOG_FILE})")
        ctk.CTkLabel(log_header, textvariable=self.log_path_var, text_color="gray",
                      font=ctk.CTkFont(size=10)).pack(side=tk.LEFT, padx=8)

        ctk.CTkButton(log_header, text="检查更新", command=self._check_update,
                       width=80).pack(side=tk.RIGHT, padx=(0, 6))
        ctk.CTkButton(log_header, text="清空日志", command=self._clear_log,
                       width=80).pack(side=tk.RIGHT, padx=(0, 4))

        log_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        log_frame.pack(fill=tk.BOTH, expand=True, **pad)

        self.log_widget = ctk.CTkTextbox(
            log_frame, wrap="word", state="disabled",
            font=ctk.CTkFont(family="Consolas", size=11),
            fg_color="#111111", text_color="#d4d4d4",
        )
        self.log_widget.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        # Tag colors
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

            self.log_widget.configure(state="normal")
            self.log_widget.insert(tk.END, msg + "\n", tag)
            self.log_widget.see(tk.END)
            self.log_widget.configure(state="disabled")

        self.root.after(100, self._poll_logs)

    # ── Codex profile ────────────────────────────────────────

    def _choose_workdir(self):
        initial = self._resolved_workdir()
        chosen = filedialog.askdirectory(
            title="选择 Codex 工作目录",
            initialdir=str(initial),
        )
        if chosen:
            self.workdir_var.set(chosen)
            self._save_settings()

    def _resolved_workdir(self) -> Path:
        raw = self.workdir_var.get().strip()
        path = Path(raw).expanduser() if raw else Path.home()
        if not path.exists() or not path.is_dir():
            return Path.home()
        return path

    def _profile_config_text(self, port: int | None = None) -> str:
        p = port if port is not None else self.port_var.get()
        return (
            'model_provider = "OpenAI"\n'
            'model = "gpt-5.4"\n'
            'review_model = "gpt-5.4"\n'
            'model_reasoning_effort = "high"\n'
            "disable_response_storage = true\n"
            f'openai_base_url = "http://127.0.0.1:{p}/v1"\n'
            "allow_insecure = true\n"
            "\n"
            "[model_providers.OpenAI]\n"
            'name = "OpenAI"\n'
            f'base_url = "http://127.0.0.1:{p}/v1"\n'
            'wire_api = "responses"\n'
            "requires_openai_auth = true\n"
        )

    def _profile_auth_text(self) -> str:
        return json.dumps({"OPENAI_API_KEY": "proxy-to-codex"}, ensure_ascii=False)

    def _write_codex_profile(self) -> None:
        CODEX_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

        port = self.port_var.get()
        new_url = f"http://127.0.0.1:{port}/v1"

        if CODEX_PROFILE_CONFIG.exists():
            content = CODEX_PROFILE_CONFIG.read_text(encoding="utf-8")
            try:
                doc = tomlkit.parse(content)
            except Exception:
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = CODEX_PROFILE_CONFIG.with_suffix(f".toml.bak.{timestamp}")
                shutil.copy2(CODEX_PROFILE_CONFIG, backup_path)
                logger.warning(
                    f"代理版 Codex config.toml 解析失败，已备份至 {backup_path.name}，将重建最小配置。"
                )
                doc = tomlkit.document()
        else:
            doc = tomlkit.document()

        # Upsert proxy-owned top-level fields
        doc["model_provider"] = "OpenAI"
        doc["model"] = "gpt-5.4"
        doc["review_model"] = "gpt-5.4"
        doc["model_reasoning_effort"] = "high"
        doc["disable_response_storage"] = True
        doc["openai_base_url"] = new_url
        doc["allow_insecure"] = True

        # Upsert [model_providers.OpenAI] section
        if "model_providers" not in doc:
            doc["model_providers"] = tomlkit.table()
        providers = doc["model_providers"]
        if "OpenAI" not in providers:
            providers["OpenAI"] = tomlkit.table()
        openai_section = providers["OpenAI"]
        openai_section["name"] = "OpenAI"
        openai_section["base_url"] = new_url
        openai_section["wire_api"] = "responses"
        openai_section["requires_openai_auth"] = True

        CODEX_PROFILE_CONFIG.write_text(tomlkit.dumps(doc), encoding="utf-8")

        CODEX_PROFILE_AUTH.write_text(
            self._profile_auth_text(),
            encoding="utf-8",
        )
        logger.info(f"代理版 Codex profile 已写入: {CODEX_PROFILE_DIR}")

    def _build_launch_script(self) -> str:
        workdir = self._resolved_workdir()
        profile = CODEX_PROFILE_DIR
        return "\n".join([
            f"cd {self._ps_quote(str(workdir))}",
            f"$env:CODEX_HOME = {self._ps_quote(str(profile))}",
            "$env:OPENAI_API_KEY = 'proxy-to-codex'",
            "codex",
        ])

    def _build_launch_command_text(self) -> str:
        return self._build_launch_script().replace("\n", "; ")

    @staticmethod
    def _ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    def _copy_to_clipboard(self, content: str, success_message: str):
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        logger.info(success_message)
        messagebox.showinfo("已复制", success_message)

    def _open_folder(self, path: Path):
        target = path if path.is_dir() else path.parent
        try:
            if os.name == "nt":
                os.startfile(str(target))
            else:
                webbrowser.open(target.as_uri())
        except Exception as e:
            logger.error(f"打开文件夹失败: {e}\n{traceback.format_exc()}")
            messagebox.showerror("错误", f"打开文件夹失败:\n{e}")

    def _copy_launch_command(self):
        try:
            self._write_codex_profile()
            command = self._build_launch_command_text()
            self._save_settings()
            self._copy_to_clipboard(command, "代理版 Codex 启动命令已复制到剪贴板。")
        except Exception as e:
            logger.error(f"复制启动命令失败: {e}\n{traceback.format_exc()}")
            messagebox.showerror("错误", f"复制启动命令失败:\n{e}")

    def _launch_codex(self):
        if not self.server_running:
            messagebox.showwarning("服务器未启动", "请先启动代理服务器。")
            return

        try:
            self._write_codex_profile()
            self._save_settings()
            script = self._build_launch_script()
            wt = shutil.which("wt")
            if wt:
                subprocess.Popen([wt, "powershell", "-NoExit", "-Command", script])
            else:
                subprocess.Popen(["powershell", "-NoExit", "-Command", script])
            logger.info(f"已打开代理版 Codex，工作目录: {self._resolved_workdir()}")
        except Exception as e:
            logger.error(f"打开代理版 Codex 失败: {e}\n{traceback.format_exc()}")
            messagebox.showerror("错误", f"打开代理版 Codex 失败:\n{e}")

    def _update_codex_actions_state(self):
        state = "normal" if self.server_running else "disabled"
        self.launch_codex_btn.configure(state=state)

    def _view_profile_config(self):
        try:
            if CODEX_PROFILE_CONFIG.exists():
                content = CODEX_PROFILE_CONFIG.read_text(encoding="utf-8")
            else:
                content = self._profile_config_text()
            if CODEX_PROFILE_AUTH.exists():
                auth_content = CODEX_PROFILE_AUTH.read_text(encoding="utf-8")
            else:
                auth_content = self._profile_auth_text()
            self._show_config_viewer(
                "代理版 Codex 配置",
                str(CODEX_PROFILE_DIR),
                f"{CODEX_PROFILE_CONFIG}\n\n{content}\n\n{CODEX_PROFILE_AUTH}\n\n{auth_content}",
                CODEX_PROFILE_DIR,
            )
        except Exception as e:
            messagebox.showerror("错误", f"无法读取代理配置:\n{e}")

    # ── Model settings popup ─────────────────────────────────

    def _open_model_settings(self):
        dialog = ctk.CTkToplevel(self.root)
        dialog.title("模型设置")
        dialog.resizable(True, True)
        dialog.minsize(480, 300)
        dialog.geometry("560x400")
        dialog.transient(self.root)
        dialog.grab_set()

        edit_map = dict(self.model_map)
        rows: list[dict] = []

        # Scrollable area using CTkScrollableFrame
        scroll_frame = ctk.CTkScrollableFrame(dialog, label_text="别名 → DeepSeek 模型")
        scroll_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(12, 4))

        def _add_row(alias: str = "", ds_model: str = ""):
            row_frame = ctk.CTkFrame(scroll_frame, fg_color="transparent")
            row_frame.pack(fill=tk.X, pady=2)

            alias_entry = ctk.CTkEntry(row_frame, width=170, placeholder_text="别名")
            alias_entry.insert(0, alias)
            alias_entry.pack(side=tk.LEFT, padx=(0, 4))

            ctk.CTkLabel(row_frame, text="→").pack(side=tk.LEFT, padx=4)

            model_entry = ctk.CTkEntry(row_frame, width=230, placeholder_text="DeepSeek 模型")
            model_entry.insert(0, ds_model)
            model_entry.pack(side=tk.LEFT, padx=4)

            remove_btn = ctk.CTkButton(
                row_frame, text="✕", width=32, fg_color="#b33",
                hover_color="#d44",
                command=lambda r=row_frame: _remove_row(r),
            )
            remove_btn.pack(side=tk.LEFT, padx=(4, 0))

            rows.append({
                "frame": row_frame,
                "alias": alias_entry,
                "model": model_entry,
            })

        def _remove_row(row_frame):
            for i, r in enumerate(rows):
                if r["frame"] is row_frame:
                    r["frame"].destroy()
                    del rows[i]
                    break

        for alias, ds_model in edit_map.items():
            _add_row(alias, ds_model)

        # Buttons bar
        btn_bar = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_bar.pack(fill=tk.X, padx=12, pady=(4, 12))

        ctk.CTkButton(btn_bar, text="+", width=36, command=_add_row).pack(side=tk.LEFT)

        def _on_ok():
            new_map: dict[str, str] = {}
            for r in rows:
                a = r["alias"].get().strip()
                m = r["model"].get().strip()
                if a and m:
                    new_map[a] = m
            if new_map:
                self.model_map = new_map
            dialog.destroy()

        ctk.CTkButton(btn_bar, text="确定", width=80, command=_on_ok).pack(side=tk.RIGHT, padx=4)
        ctk.CTkButton(btn_bar, text="取消", width=80, fg_color="transparent",
                       border_width=1, command=dialog.destroy).pack(side=tk.RIGHT, padx=4)

    def _load_settings(self):
        try:
            if SETTINGS_FILE.exists():
                data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
                if data.get("api_key"):
                    self.api_key_var.set(data["api_key"])
                if data.get("port"):
                    self.port_var.set(data["port"])
                if data.get("model_map") and isinstance(data["model_map"], dict):
                    self.model_map = data["model_map"]
                if data.get("base_url"):
                    self.base_url = data["base_url"]
                if data.get("workdir"):
                    self.workdir_var.set(data["workdir"])
        except Exception:
            pass
        if not self.model_map:
            self.model_map = dict(DEFAULT_MODEL_MAP)

    def _save_settings(self):
        try:
            SETTINGS_FILE.write_text(
                json.dumps({
                    "api_key": self.api_key_var.get(),
                    "port": self.port_var.get(),
                    "model_map": self.model_map,
                    "base_url": self.base_url,
                    "workdir": self.workdir_var.get(),
                }, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存设置失败: {e}")

    def _toggle_server(self):
        if self._server_starting:
            return
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

        self._server_starting = True
        self.server_btn.configure(text="停止服务器")
        self.port_entry.configure(state="disabled")
        self.key_entry.configure(state="disabled")
        self.model_settings_btn.configure(state="disabled")

        self.server_thread = threading.Thread(
            target=self._run_server, args=(port, self.model_map, self.base_url), daemon=True
        )
        self.server_thread.start()

        self.status_var.set("●  启动中…")
        self.status_label.configure(text_color="gray")
        self.url_var.set(f"http://localhost:{port}")

        logger.info(f"服务器在端口 {port} 启动中…")
        self.root.after(600, self._check_server_started, port)

    def _check_server_started(self, port: int):
        self._server_starting = False
        if self.server_thread is None:
            return
        if self.server_thread.is_alive():
            self.server_running = True
            self.status_var.set("●  运行中")
            self.status_label.configure(text_color="green")
            self._update_codex_actions_state()
        else:
            self.server_running = False
            self.server_instance = None
            self.server_thread = None
            self.status_var.set("●  已停止")
            self.status_label.configure(text_color="gray")
            self.url_var.set("")
            self.server_btn.configure(text="启动服务器")
            self.port_entry.configure(state="normal")
            self.key_entry.configure(state="normal")
            self.model_settings_btn.configure(state="normal")
            self._update_codex_actions_state()
            messagebox.showerror("启动失败", f"端口 {port} 被占用或无法绑定，请更换端口后重试。")

    def _run_server(self, port: int, model_map: dict[str, str], base_url: str):
        try:
            app = create_app(model_map=model_map, base_url=base_url)
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
            self.server_thread.join(timeout=1)

        self.server_running = False
        self.server_instance = None
        self.server_thread = None

        self.status_var.set("●  已停止")
        self.status_label.configure(text_color="gray")
        self.url_var.set("")
        self.server_btn.configure(text="启动服务器")
        self.port_entry.configure(state="normal")
        self.key_entry.configure(state="normal")
        self.model_settings_btn.configure(state="normal")

        self._update_codex_actions_state()

        logger.info("服务器已停止。")

    def _show_config_viewer(self, title: str, subtitle: str, content: str, folder_path: Path | None = None):
        viewer = ctk.CTkToplevel(self.root)
        viewer.title(title)
        viewer.geometry("780x560")
        viewer.minsize(580, 400)
        viewer.transient(self.root)

        ctk.CTkLabel(viewer, text=title, font=ctk.CTkFont(size=13, weight="bold")).pack(
            anchor=tk.W, padx=14, pady=(14, 2))
        ctk.CTkLabel(viewer, text=subtitle, text_color="gray").pack(
            anchor=tk.W, padx=14, pady=(0, 8))

        text = ctk.CTkTextbox(
            viewer,
            wrap="none",
            state="normal",
            font=ctk.CTkFont(family="Consolas", size=12),
        )
        text.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 8))
        text.insert("1.0", content)
        text.configure(state="disabled")

        footer = ctk.CTkFrame(viewer, fg_color="transparent")
        footer.pack(fill=tk.X, padx=14, pady=(0, 14))
        ctk.CTkButton(
            footer,
            text="复制配置",
            command=lambda: self._copy_to_clipboard(content, "代理配置已复制到剪贴板。"),
            width=92,
        ).pack(side=tk.LEFT)
        if folder_path is not None:
            ctk.CTkButton(
                footer,
                text="打开文件夹",
                command=lambda: self._open_folder(folder_path),
                width=92,
            ).pack(side=tk.LEFT, padx=(8, 0))
        ctk.CTkButton(footer, text="关闭", command=viewer.destroy, width=80).pack(side=tk.RIGHT)
        viewer.focus_set()

    # ── Cleanup ──────────────────────────────────────────────

    def _on_close(self):
        if self.server_running:
            self._stop_server()
        self._server_starting = False
        self.root.destroy()
        os._exit(0)

    # ── Update check ─────────────────────────────────────────

    def _check_update(self):
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
                self.root.after(0, lambda: messagebox.showwarning(
                    "检查更新", "无法获取最新版本信息。"))
                return
            current = __version__.lstrip("v")
            if latest == current:
                self.root.after(0, lambda: messagebox.showinfo(
                    "检查更新", f"当前已是最新版本 v{current}。"))
                return
            latest_parts = tuple(int(x) for x in latest.split(".") if x.isdigit())
            current_parts = tuple(int(x) for x in current.split(".") if x.isdigit())
            if latest_parts > current_parts:
                download_url = None
                filename = None
                for asset in data.get("assets", []):
                    name = asset.get("name", "")
                    if "setup" in name.lower() and name.endswith(".exe"):
                        download_url = asset.get("browser_download_url")
                        filename = name
                        break
                logger.info(f"New version available: v{latest} (current: v{current})")
                self.root.after(0, lambda: self._prompt_download(latest, download_url, filename))
            else:
                self.root.after(0, lambda: messagebox.showinfo(
                    "检查更新", f"当前已是最新版本 v{current}。"))
        except Exception:
            self.root.after(0, lambda: messagebox.showwarning(
                "检查更新", "检查更新失败，请检查网络连接后重试。"))

    def _prompt_download(self, latest: str, download_url: str | None, filename: str | None):
        if not download_url:
            release_url = f"https://github.com/fadeawaylove/proxy-to-codex/releases/tag/v{latest}"
            messagebox.showinfo(
                "发现新版本",
                f"新版本 v{latest} 已发布！\n\n下载地址：\n{release_url}",
            )
            return
        if messagebox.askyesno(
            "发现新版本",
            f"新版本 v{latest} 已发布！\n\n是否下载并安装？",
        ):
            self._download_and_install(download_url, filename)

    def _download_and_install(self, url: str, filename: str):
        import tempfile

        dest = Path(tempfile.gettempdir()) / filename

        progress_win = ctk.CTkToplevel(self.root)
        progress_win.title("下载更新")
        progress_win.geometry("440x160")
        progress_win.resizable(False, False)
        progress_win.transient(self.root)
        progress_win.grab_set()

        ctk.CTkLabel(progress_win, text=f"正在下载: {filename}").pack(padx=16, pady=(16, 8))

        progress_bar = ctk.CTkProgressBar(progress_win, width=380)
        progress_bar.set(0)
        progress_bar.pack(padx=16, pady=4)

        percent_var = tk.StringVar(value="0%")
        ctk.CTkLabel(progress_win, textvariable=percent_var).pack(pady=4)

        def do_download():
            try:
                import urllib.request
                import ssl

                ctx = ssl.create_default_context()
                req = urllib.request.Request(url, headers={"User-Agent": "proxy-to-codex"})
                with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:
                    total = int(resp.headers.get("Content-Length", 0))
                    downloaded = 0
                    with open(dest, "wb") as f:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            f.write(chunk)
                            downloaded += len(chunk)
                            if total:
                                pct = downloaded / total
                                progress_win.after(
                                    0,
                                    lambda p=pct: (
                                        progress_bar.set(p),
                                        percent_var.set(f"{p:.0%}"),
                                    ),
                                )
                progress_win.after(0, on_done)
            except Exception as e:
                progress_win.after(0, lambda: on_error(str(e)))

        def on_done():
            percent_var.set("下载完成，正在启动安装程序…")
            progress_bar.configure(mode="indeterminate")
            progress_bar.start()
            logger.info(f"Downloaded {filename}, launching installer…")
            try:
                subprocess.Popen([str(dest)])
                progress_win.destroy()
                self._on_close()
            except Exception as e:
                logger.error(f"Failed to launch installer: {e}")
                messagebox.showerror("错误", f"启动安装程序失败:\n{e}")
                progress_win.destroy()

        def on_error(msg: str):
            logger.error(f"Download failed: {msg}")
            messagebox.showerror("下载失败", f"下载更新失败:\n{msg}")
            progress_win.destroy()

        threading.Thread(target=do_download, daemon=True).start()

    def _clear_log(self):
        self.log_widget.configure(state="normal")
        self.log_widget.delete("1.0", tk.END)
        self.log_widget.configure(state="disabled")
        logger.debug("日志窗口已清空")


# ── Main entry ──────────────────────────────────────────────
def main():
    root = ctk.CTk()

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
