# 单会话总结 Prompt

用户指定了一个会话文件夹做一次性总结。所有数据路径都相对于包含 `config.toml` 的工作区；代码从 skill 根加载。

## 步骤

1. **预处理**：先把 skill 根加入 `PYTHONPATH`，再从工作区运行：

   ```powershell
   $env:PYTHONPATH = "<skill-root>"
   python -c "from wechat_diary_core import archive; archive(r'WeFlow-raw-exports/{folder}')"
   ```

   把 `{folder}` 替换为用户指定的文件夹名。若配置中的 raw 根不是默认值，使用 `[paths].raw` 解析后的实际路径。

2. **读取 processed**：预处理后从配置指定的 processed 根读取结果。如果该会话已有 md 文件，直接读取并跳过步骤 1。

3. **写出总结**：在配置指定的 insights 根创建 `Summaries/{folder}__{timestamp}.md`，其中 `{timestamp}` 为当前时间，格式 `yyyymmdd-HHMMSS`。

## 输出结构

```markdown
# {folder} 总结

## 概要
<!-- 2-3 句话概括这个会话的主题、参与者和时间跨度。 -->

## 主题梳理
<!-- 按主题而非时间线组织。每个主题一个三级标题：
### 主题名
- 要点 1
- 要点 2
提炼讨论的结论、决定和分歧。 -->

## 待办与后续
<!-- 提取行动项、未解决的问题和后续安排。
- 行动项描述。涉及：<谁>
没有则写“无”。 -->
```

## 规则

- 按主题组织，不按时间线流水账。
- 优先提炼结论和决定，而非讨论过程。
- 每个主题下的要点用一句话概括。
- 不编造没有讨论过的内容。
