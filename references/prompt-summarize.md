# 单会话总结 Prompt

用户指定一个会话做一次性总结（不是日常二加）。所有数据路径相对于含 `config.toml` 的工作区。

**读 processed 之前先读 [processed-format.md](processed-format.md)**——占位符（引用两态、媒体路径、具名表情、文件有名/无名）的唯一事实源。

## 第一步：把语料弄到手（两条路，按记录在不在盘上选）

### A. 记录已在盘上 → 消费已有 raw

适用：日常导出已经覆盖过这个会话、用户给了一份旧导出、或上次按需导出的产物还在。

```powershell
python "<skill-root>\scripts\process_existing_raw.py" --raw-root <raw目录> --day <yyyy-mm-dd> --require-day
```

若该会话的 processed md 已经存在，直接读，这一步可整个跳过。

### B. 记录还没导出 → 按需导出

适用：要某个会话某个时间段的完整记录，而本地没有。

```powershell
python "<skill-root>\scripts\export_on_demand.py" --session <wxid或显示名子串> --start <起> --end <止> --out <输出目录> --merged
```

- 会话名拿不准先 `--list-sessions <关键词>` 列候选。
- 只写 `--out` 目录，**不碰** live 的 raw / processed / 归档。
- 跨度较长时 `--merged` 会额外给一份整段合并 markdown，通读更省事。
- 需要 WeFlow 在跑且 API 服务已启用；不确定先跑 `doctor.py`。

参数详表见 [entrypoints.md](entrypoints.md) §2 / §3。

## 第二步：写出总结

产物是一个**目录**：`<insights根>/Summaries/<会话名>__<yyyymmdd-HHMMSS>/`

```text
Summaries/<会话名>__<时间戳>/
├── 总结.md                    # 必出：概要 + 主题梳理 + 待办与后续
└── topics/                    # 可选：会话跨度大、主题多时才拆
    └── T001__<主题名>.md
```

规则：

- **`总结.md` 必出**，结构见下。会话很小时只出这一个文件即可。
- **`topics/` 按需**：只有当主题多到一份文件读不动时才拆。编号 `T001` 起，文件名 `T<序号>__<主题名>.md`。
- **过程产物不进 insights**：逐条 LLM 调用记录、checkpoint、中间态 json 落工作区临时目录或 `tests/_artifacts/`，不要写进 `Summaries/`。insights 只放成品。

## `总结.md` 结构

```markdown
# {会话名} 总结

> 范围：{起始日期} ~ {结束日期} · 消息数约 {N} · 生成于 {yyyy-mm-dd}

## 概要
<!-- 2-3 句话：这个会话的主题、参与者、时间跨度。 -->

## 主题梳理
<!-- 按主题而非时间线组织。每个主题一个三级标题：
### 主题名
- 要点 1
- 要点 2
提炼讨论的结论、决定和分歧。主题拆成 topics/ 时，这里只留一句索引 + 链接。 -->

## 待办与后续
<!-- 行动项、未解决的问题、后续安排。
- 行动项描述。涉及：<谁>
没有则写「无」。 -->
```

## 规则

- 引用原话 ≤15 字并加引号，不要大段搬运聊天记录。
- 群聊里与用户无关的闲聊可以整段略过，不必强行覆盖全部消息。
- 拿不准会话时间跨度时，先看 processed 目录下有哪些日期文件，不要假设。
