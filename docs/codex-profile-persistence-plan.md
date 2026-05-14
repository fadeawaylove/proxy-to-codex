# 修复代理版 Codex Profile 的偏好持久化

## Summary

- 根因已经确认：不是 Codex “没记住”，而是 `proxy-to-codex` 每次打开代理版 Codex 前都会重写独立 profile 的 `config.toml`。
- 目前这个独立 profile 里，Codex 会把目录信任和沙盒偏好写进去，例如：
  - `[projects.'c:\code\cli-proxy-api-management-center'] trust_level = "trusted"`
  - `[windows] sandbox = "elevated"`
- 但 `main.py` 的 `_write_codex_profile()` 现在是整文件覆盖，所以这些设置每次启动前都会被抹掉，导致再次询问信任目录和沙盒。

## Key Changes

- 把代理 profile 的写入方式从“整文件覆盖”改成“保留现有配置、只更新代理字段”：
  - 使用 `tomlkit` 读取 `%APPDATA%\proxy-to-codex\codex-profile\config.toml`。
  - 仅 upsert 代理拥有的字段：
    - `model_provider`
    - `model`
    - `review_model`
    - `model_reasoning_effort`
    - `disable_response_storage`
    - `openai_base_url`
    - `allow_insecure`
    - `[model_providers.OpenAI]` 下的 `name/base_url/wire_api/requires_openai_auth`
  - 保留所有非代理字段与表，不删除、不重排业务无关配置，尤其保留：
    - `[projects.*]`
    - `[windows]`
    - `[tui.*]`
    - Codex 后续自己写入的其他状态

- 明确代理配置的“所有权边界”：
  - `proxy-to-codex` 只负责代理连通所需的 OpenAI provider 配置和占位 `auth.json`。
  - Codex 自己写入的信任目录、沙盒、TUI 状态、插件缓存等，都视为用户/CLI 自有状态，不再覆盖。

- 增强容错：
  - 若 `config.toml` 不存在，则创建最小代理配置。
  - 若存在但 TOML 解析失败，则将原文件备份为带时间戳的 `.bak`，再写入最小代理配置，并在 GUI 日志中明确提示。
  - `auth.json` 继续只写占位 `OPENAI_API_KEY`；若将来发现 Codex 往 `auth.json` 写别的必要状态，再单独评估是否同样改为 merge。

- 文案与说明同步：
  - README 改成明确说明“代理版 profile 会保留 Codex 自己记住的目录信任和沙盒偏好，不再每次重置”。
  - “查看代理配置”继续展示最终合并后的实际配置，而不是仅展示模板文本。

## Test Plan

- 单元/轻量集成验证：
  - 现有 `config.toml` 只含代理字段时，写入后结果不变。
  - 现有 `config.toml` 含 `[projects.*].trust_level`、`[windows].sandbox` 时，更新端口后这些字段仍保留。
  - 现有 `config.toml` 含额外 `[tui.*]` 或其他未知表时，更新后仍保留。
  - 无 `config.toml` 时可生成完整最小代理配置。
  - 非法 TOML 时会备份并重建。

- 手动验证：
  - 首次打开代理版 Codex，信任目录并设置沙盒。
  - 关闭后再次从 GUI 打开同一工作目录，不再重复询问。
  - 修改 GUI 端口并再次启动，代理 URL 更新，但信任/沙盒配置仍在。
  - “查看代理配置”里能看到 `[projects.*]` 和 `[windows]` 仍存在。

- 回归检查：
  - `uv run python -m py_compile main.py server.py codex_config.py`
  - 现有代理启动、复制启动命令、打开终端流程不受影响。

## Assumptions

- 信任目录与沙盒偏好以 `config.toml` 为主存储，当前已从实际 profile 验证成立。
- 仍继续使用独立 `CODEX_HOME=%APPDATA%\proxy-to-codex\codex-profile`，不回退到修改真实 `~/.codex`。
- 代理拥有的 OpenAI provider 配置以当前 `OpenAI` 名称为准，不额外引入新的 provider 名称或更复杂的多 provider 合并策略。
