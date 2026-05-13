# proxy-to-codex

将 OpenAI Codex CLI 的 Responses API 请求转换为 DeepSeek Chat Completions API 的本地代理，让 Codex 可以使用 DeepSeek 模型。

## 特点

- **GUI 管理** — 图形界面一键启停，实时日志查看
- **模型映射可配置** — 自定义 GPT 别名 → DeepSeek 模型的映射关系，想用哪个模型自己定
- **WebSocket 流式** — 支持 Codex 的流式对话，实时返回
- **HTTP 回退** — 非流式请求自动走 HTTP 通道
- **独立 Codex Profile** — 不修改本机真实 Codex 配置，用临时 profile 启动代理版 Codex
- **快捷启动** — 一键打开已配置代理的 Codex 终端，也可复制启动命令
- **端口检测** — 启动失败（如端口被占用）时弹窗提醒，不会假死

## 安装

### 方式一：下载安装程序（Windows）

从 [Releases](https://github.com/fadeawaylove/proxy-to-codex/releases) 下载 `proxy-to-codex_setup_v*.exe`，双击安装。

### 方式二：用 uv 运行

```bash
# 克隆仓库
git clone https://github.com/fadeawaylove/proxy-to-codex.git
cd proxy-to-codex

# 安装依赖并启动 GUI
uv sync
uv run python main.py
```

### 方式三：无头模式（不用 GUI）

```bash
export DEEPSEEK_API_KEY="你的API密钥"
uv run uvicorn server:app --host 0.0.0.0 --port 43214
```

然后用环境变量或直接编辑 `server.py` 的模型映射。

## 使用方式

### 1. 启动代理

打开 GUI → 填入 DeepSeek API 密钥 → 点击「启动服务器」。

状态显示 `● 运行中` 即启动成功。

### 2. 配置模型映射

点击「模型设置」按钮，在弹窗中编辑别名和 DeepSeek 模型的对应关系：

```
gpt-5.4      → deepseek-v4-pro
gpt-4o       → deepseek-v4-flash
...
```

可自由增删行，点「确定」保存。下次启动生效。

### 3. 打开代理版 Codex

服务器启动后，在「Codex 使用方式」里选择 Codex 的工作目录，然后点击「打开代理版 Codex」。

程序会创建自己的临时 profile：

```text
%APPDATA%\proxy-to-codex\codex-profile\config.toml
%APPDATA%\proxy-to-codex\codex-profile\auth.json
```

`config.toml` 内容类似：

```toml
model_provider = "OpenAI"
model = "gpt-5.4"
review_model = "gpt-5.4"
model_reasoning_effort = "high"
disable_response_storage = true
openai_base_url = "http://127.0.0.1:43214/v1"
allow_insecure = true

[model_providers.OpenAI]
name = "OpenAI"
base_url = "http://127.0.0.1:43214/v1"
wire_api = "responses"
requires_openai_auth = true
```

`auth.json` 只写入一个本地占位 key：

```json
{"OPENAI_API_KEY":"proxy-to-codex"}
```

随后会在新终端中设置 `CODEX_HOME` 指向这个 profile，并启动 `codex`。你的真实 `~/.codex/config.toml` 不会被修改。

也可以点击「复制启动命令」，手动在终端运行。

## 工作原理

```
Codex CLI ──→ proxy-to-codex (:43214) ──→ DeepSeek API
              ↑ 翻译 Responses API       ↑ Chat Completions API
              为 Chat Completions
```

代理在本地监听，将 Codex 发出的 OpenAI Responses API 格式请求转换为 DeepSeek 的 Chat Completions API 格式，流式 SSE 响应也做了对应转换。

## 设置文件

所有设置保存在 `%APPDATA%\proxy-to-codex\settings.json`：

```json
{
  "api_key": "你的密钥",
  "port": 43214,
  "model_map": {
    "gpt-5.4": "deepseek-v4-pro",
    "gpt-4o": "deepseek-v4-flash"
  },
  "base_url": "https://api.deepseek.com/v1",
  "workdir": "C:\\Users\\you"
}
```

代理版 Codex profile：`%APPDATA%\proxy-to-codex\codex-profile\config.toml`

日志文件：`%APPDATA%\proxy-to-codex\logs\proxy.log`

## 构建

```bash
uv sync
uv run pyinstaller --clean --noconfirm proxy-to-codex.spec
```

安装程序用 Inno Setup 打包：

```bash
iscc /DMyAppVersion=0.1.17 scripts\setup.iss
```
