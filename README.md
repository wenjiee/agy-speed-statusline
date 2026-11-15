# agy-speed-statusline

极简的 Antigravity CLI (`agy`) 自定义 `statusLine` 脚本：完整保留原生最左侧（`? for shortcuts`）和最右侧（含 `auto` 琥珀金高亮、模型名与推理强度），仅在底栏正中间空白处显示 **首字延迟（TTFT）与吐字速度（t/s）**。

```text
4.0s · 507 t/s │ 均 7.2s · 284 t/s
```

## 特性

- **零额外占用**：纯 Python 标准库（`sqlite3`、`struct`、`unicodedata`），冷启动执行约 40–50ms，无第三方依赖。
- **不破坏原生底栏**：1:1 还原 `? for shortcuts` 与 `auto · Model · effort` 原生配色（`auto` 保持 `#F9AB00` 琥珀金，其余保持 `#9AA0A6`）。
- **双色层级**：分隔符 `·`、`│`、`均` 暗灰弱化，首字延迟浅青，吐字速度亮白。
- **自动响应式**：窄屏（如 80 列）自动降级为单轮格式 `4.0s · 507 t/s`，不换行不遮挡。
- **直读本地只读库**：以 `?mode=ro` 只读打开 `~/.gemini/antigravity-cli/conversations/<conversation_id>.db` 的 `gen_metadata` protobuf（支持 `AGY_CLI_DIR` 环境变量覆盖）。

## 指标计算方式

- **左侧数值（最近一轮）**：最近一次模型生成记录的 `time_to_first_token`（首字延迟）与 `output_tokens / streaming_duration`（流式吐字速度，过滤 `< 0.2s` 瞬时缓存/工具响应以避免失真）。
- **右侧均值（全程平均）**：当前会话所有有效轮次的平均首字延迟，以及按流式耗时加权的总吐字速度（`sum(output_tokens) / sum(streaming_duration)`）。

## 使用方法

1. 克隆仓库并赋予执行权限：

```bash
git clone https://github.com/wenjiee/agy-speed-statusline.git
chmod +x agy-speed-statusline/statusline.py
```

2. 在 `~/.gemini/antigravity-cli/settings.json` 中配置 `statusLine`（或在 `agy` 对话中运行 `/statusline python3 /path/to/agy-speed-statusline/statusline.py`）：

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /path/to/agy-speed-statusline/statusline.py"
  }
}
```

3. 如需恢复 `agy` 默认状态栏，在对话中运行 `/statusline delete` 即可。

## License

MIT
