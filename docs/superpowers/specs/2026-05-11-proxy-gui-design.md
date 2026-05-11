# Proxy to Codex — GUI + HTTP Fix

**Date:** 2026-05-11
**Status:** implemented

## Overview

A local proxy server that translates OpenAI Codex's Responses API (WebSocket + HTTP) into
DeepSeek's Chat Completions API. Includes a Tkinter desktop GUI for managing the server,
configuring port, and auto-configuring Codex's config file to point at the proxy.

## Why

Codex (OpenAI's coding agent CLI) speaks the Responses API. DeepSeek speaks Chat Completions.
This proxy sits between them so Codex can use DeepSeek models without Codex needing to know
about the translation.

Additionally, Codex falls back from WebSocket to HTTP POST when the WebSocket connection
fails, but the original implementation only handled WebSocket — hence `POST /v1/responses`
returned 404.

## Package Management

This project uses **[uv](https://docs.astral.sh/uv/)** (an extremely fast Python package and
project manager written in Rust) for dependency management and virtual environment handling.

- **Dependencies** are declared in `pyproject.toml` under `[project].dependencies`
- **Lock file** is `uv.lock` (cross-platform, reproducible installs)
- **Virtual env** is managed by uv at `.venv/`
- **Python version** is pinned in `.python-version` (>=3.12)

### Common uv commands

```bash
uv sync           # install/update all dependencies, create .venv if missing
uv add <pkg>      # add a runtime dependency
uv run python main.py   # run the GUI
uv run uvicorn server:app --host 0.0.0.0 --port 4000   # run server headless (no GUI)
```

uv was chosen over pip/poetry for speed, reliable lockfile, and zero-config virtualenvs.

## Architecture

```
┌──────────────┐   HTTP POST /v1/responses   ┌──────────────────┐   Chat Completions   ┌──────────────┐
│  Codex CLI   │ ───────────────────────────→ │  proxy-to-codex  │ ────────────────────→ │  DeepSeek    │
│              │ ←─────────────────────────── │  localhost:PORT   │ ←──────────────────── │  API         │
│  (编辑器)     │    Responses JSON            │                  │    SSE / JSON         │              │
└──────────────┘                             └────────┬─────────┘                       └──────────────┘
                                                      │
                                                      │ Tkinter main thread
                                                      ▼
                                             ┌──────────────────┐
                                             │   GUI 管理面板    │
                                             │  · 端口 / API Key │
                                             │  · 启动 / 停止     │
                                             │  · Codex 配置管理  │
                                             │  · 实时日志        │
                                             └──────────────────┘
```

## Components

### `server.py` — ASGI application

Pure FastAPI app, no GUI dependency. Exports `create_app()` for programmatic use.

**Endpoints:**

| Method | Path | Description |
|--------|------|-------------|
| WebSocket | `/v1/responses` | Streaming Responses API → Chat Completions SSE |
| POST | `/v1/responses` | Non-streaming Responses API (fixes 404) |
| GET | `/v1/models` | Model list (gpt-5.4, gpt-5.5, gpt-4o, ...) |
| POST | `/v1/chat/completions` | Direct Chat Completions pass-through |
| GET | `/health` | Health check |

**Key functions:**

- `translate_response_create_to_chat(body)` → chat_body, response_id, meta
- `translate_sse_chunk_to_ws_events(sse_data, state, meta)` → list of WS events
- `_translate_chat_response_to_responses(chat_response, response_id, meta)` → Responses API JSON
- `SessionStore` — in-memory TTL-based session store for `previous_response_id` chaining

**HTTP POST `/v1/responses` flow:**

1. Parse JSON body (accepts both wrapped `{type: "response.create", ...}` and unwrapped)
2. Translate → Chat Completions body via `translate_response_create_to_chat()`
3. Call DeepSeek non-streaming (`stream: false`)
4. Translate response back to Responses API format via `_translate_chat_response_to_responses()`
5. Store messages in SessionStore for chaining
6. Return JSON

### `codex_config.py` — Codex configuration management

Manages Codex's `config.toml` file (`~/.codex/config.toml` or `%USERPROFILE%\.codex\config.toml`).

**Functions:**

- `get_config_path()` — detect config file location (env `CODEX_CONFIG_DIR` → OS-specific default)
- `backup(path)` → backup_path — create timestamped backup (`config.toml.backup.YYYYMMDD_HHMMSS`)
- `apply(port, path)` — set `[api] base_url = "http://localhost:{port}/v1"`, auto-creates config dir
- `restore_latest(path)` — restore from most recent backup
- `list_backups(path)` — list all backups, newest first

### `main.py` — Tkinter desktop GUI

Tkinter-based control panel. Zero additional dependencies (tkinter is part of Python stdlib).

**Thread model:**

- **Main thread:** Tkinter event loop (`root.mainloop()`)
- **Background thread:** uvicorn server (`daemon=True`)
- **Communication:** `queue.Queue` for log messages from server → GUI

**GUI layout:**

```
┌───────────────────────────────────────────┐
│  Server Settings                          │
│  Port: [4000]  DeepSeek API Key: [****]   │
├───────────────────────────────────────────┤
│  [Start Server] [Stop Server] ● Stopped   │
├───────────────────────────────────────────┤
│  Codex Configuration                      │
│  Config: C:\Users\...\.codex\config.toml  │
│  Status: Default (OpenAI API)             │
│  [Backup Config] [Auto-Configure] [Restore]│
├───────────────────────────────────────────┤
│  Log                                      │
│  10:30:01  INFO     Server starting...    │
│  10:30:01  INFO     Uvicorn running on... │
└───────────────────────────────────────────┘
```

**Config management workflow:**

1. User clicks "Backup Config" → creates timestamped backup
2. User clicks "Auto-Configure Codex" → auto-backup + sets proxy URL
3. User clicks "Restore Backup" → restores latest backup

## Dependencies (pyproject.toml)

```toml
[project]
name = "proxy-to-codex"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.124.4",
    "httpx>=0.28.1",
    "litellm[proxy]>=1.83.14",
    "uvicorn>=0.33.0",
]
```

No GUI-specific dependencies — Tkinter is bundled with CPython.

## Error handling

- **Upstream errors** (DeepSeek 4xx/5xx) → returned as `response.failed` (WS) or `502` JSON (HTTP)
- **Invalid JSON** → 400 JSON error
- **Timeout** → `response.failed` with timeout code (WS) or `504` JSON (HTTP)
- **GUI fatal errors** → `messagebox.showerror` then `sys.exit(1)`
- **Config file missing** → auto-create directory, disable backup button

## Future considerations

- LiteLLM integration: currently `litellm_config.yaml` exists but unused by the core proxy
- System tray mode: minimize to tray instead of closing
- Hot-reload on config change
- Streaming support for HTTP POST `/v1/responses`
