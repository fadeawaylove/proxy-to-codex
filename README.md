# proxy-to-codex

将 OpenAI Codex CLI 的 Responses API 请求转换为 DeepSeek Chat Completions API 的本地代理，让 Codex 可以使用 DeepSeek 模型。

## 特点

- **GUI 管理** — 图形界面一键启停，实时日志查看
- **模型映射可配置** — 自定义 GPT 别名 → DeepSeek 模型的映射关系，想用哪个模型自己定
- **WebSocket 流式** — 支持 Codex 的流式对话，实时返回
- **HTTP 回退** — 非流式请求自动走 HTTP 通道
- **Codex 配置自动管理** — 一键启用/关闭代理，自动备份和恢复 Codex 的 `config.toml`
- **WSL 支持** — 自动检测 WSL 中的 Codex 配置，可分别启用
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

### 3. 启用代理

服务器启动后，「Windows 代理」/「WSL 代理」按钮变为可用。点击后自动修改 Codex 的配置，将其 API 请求指向本代理。

关闭代理时会自动恢复原来的配置。

### 4. 使用 Codex

代理启用后，在终端直接使用 Codex CLI 即可，请求会自动走 DeepSeek。

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
  "base_url": "https://api.deepseek.com/v1"
}
```

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
