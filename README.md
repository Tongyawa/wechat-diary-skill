# WeChat Diary

WeChat Diary 是一个本地微信日记工作流：通过 WeFlow 导出微信聊天记录，把原始导出清洗成更适合 Agent 阅读的 Markdown，再由 `wechat-diary-skill` 生成日记、DoneList、灵感、额外观察，并增量维护长期自我画像和年度主线脉络。

代码仓与数据工作区相互独立：本仓只放可分发代码和根级 `SKILL.md`；`config.toml`、`WeFlow-*` 数据和工作区入口保留在用户自己的工作区。

## 解决什么问题

微信聊天记录里有大量个人经历、待办、灵感和长期线索，但原始导出不适合直接阅读或长期检索。本项目把它拆成两层：

1. **工程层**：自动化 WeFlow 导出、清洗、压缩群聊上下文、OCR 图片、归档 raw/processed 数据。
2. **Agent 层**：读取 processed Markdown，生成每日可读产物，并把长期信号沉淀进画像和主线文件。

最终目标不是备份微信，而是把每天散落在聊天、文件传输助手和朋友圈里的信息转成可回看的个人知识库。

## 当前主要产物

默认 `wechat-diary-skill` 会在配置指定的 insights 根下生成：

- `Diary/<yyyy>/...md`：第一人称日记。
- `DoneList/<yyyy>/...md`：当天完成事项。
- `Inspirations/<yyyy>/...md`：项目灵感、待跟进事项、值得深入的话题。
- `ExtraNotes/<yyyy>/...md`：Agent 额外观察到、但用户可能没注意到的点。
- `Profile/自我画像.md`：长期自我画像，增量维护。
- `Threads/<yyyy>.md`：年度主线脉络，增量维护。

## 目录结构

```text
.
├── SKILL.md                       # 根级 skill 入口与工作流
├── references/                    # 按需加载的二次加工 Prompt
├── docs/                          # 工程文档
├── scripts/                       # 日常入口、补跑入口、归档和校验脚本
├── tests/                         # 单元测试
├── wechat_diary_core/             # 导出、清洗、归档、配置等核心代码
├── requirements.txt               # Python 依赖
└── config.example.toml            # 工作区配置模板
```

运行时还会用到这些 gitignored 数据目录，实际位置由 `config.toml [paths]` 决定：

```text
WeFlow-raw-exports/          # WeFlow 原始导出，下一轮运行前会合并进长期归档
WeFlow-processed-exports/    # 清洗后的当日 Markdown，供 skill 读取
WeFlow-archived-exports/     # 长期 raw/processed 归档库
WeFlow-insights/             # 日记、DoneList、灵感、画像、主线等最终产物
```

## 初次配置

先把本仓安装到 Skill 目录，再创建一个独立的数据工作区。下面用 `$SkillRoot` 和 `$Workspace` 区分二者：

1. 安装 Python 依赖：

   ```powershell
   python -m pip install -r "$SkillRoot\requirements.txt"
   ```

2. 复制配置模板：

   ```powershell
   New-Item -ItemType Directory -Force $Workspace
   Copy-Item "$SkillRoot\config.example.toml" "$Workspace\config.toml"
   ```

3. 编辑 `$Workspace\config.toml`，至少确认：

   - `[export_backend].backend`：默认 `weflow_api`（WeFlow 5.x 本地 HTTP API）；已有 canonical raw、只想离线处理时可设为 `manual`。`weflow` 仅保留给 WeFlow ≤4.x 的 legacy GUI 自动化。
   - `[export_backend.weflow_api]`：确认 `base_url`，并在 WeFlow「设置 → API 服务」生成一个固定非空 Access Token 写入 `access_token`。token 不热更新，修改后要重启 API 服务；首次启用 API 服务仍需手动打开一次。
   - `[export_backend.weflow].weflow_exe`：API 不可达时可用于普通启动 WeFlow，也是 legacy GUI 后端的可执行文件路径；API 后端不会停止用户自己打开的 WeFlow。旧 `[automation]` 配置仍兼容，运行时会提示迁移。
   - `[asr].engine`：空字符串表示关闭并写明确的语音转写失败占位；设为 `sensevoice` 前按需安装 `requirements-asr.txt`。缺少可选依赖只降级语音，不阻断聊天导出。
   - `[paths]`：raw、processed、archived、insights 的落点。
   - `[user].self_wxids`：自己的 wxid / 文件传输助手，用于识别收集箱。
   - `[skills].daily`：默认包含 `wechat-diary-skill`。

`config.toml` 不进 git，适合放本机路径、账号标识和其他私人配置。

首次运行或环境变化后，可先执行只读体检：

```powershell
Push-Location $Workspace
python "$SkillRoot\scripts\doctor.py"
Pop-Location
```

doctor 会按后端检查数据入口：`weflow_api` 覆盖 `/health`、固定 token、已知非空会话的消息语义探测和 SenseVoice 可选依赖；legacy `weflow` 检查可执行文件与 CDP。此外还检查四个数据根。它不会启动 WeFlow 或修改文件。自动化联动可给同一命令追加 `--json` 获取结构化结果。

## 常用用法

每日完整导出由工作区薄壳调用本仓脚本；Agent 可直接运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\run_daily_export.ps1" -Workspace $Workspace -NoPause
```

只处理已经存在的 WeFlow raw 导出，不重新启动 WeFlow：

```powershell
Push-Location $Workspace
python "$SkillRoot\scripts\process_existing_raw.py" --raw-root WeFlow-raw-exports --day 2026-06-15 --require-day
Pop-Location
```

## 二次加工怎么发生

当前仓库没有统一的一键二次加工脚本。日常导出完成后，由 Agent 在同一工作目录按 `config.toml [skills].daily` 顺序执行 skills。

公开默认配置是：

```toml
[skills]
daily = ["wechat-diary-skill"]
```

`wechat-diary-skill` 会从工作区配置解析 processed/insights 根，跳过下划线开头的 sidecar 目录，然后按根级 `SKILL.md` 与 `references/prompt-daily.md` 的契约生成 Markdown 产物。

## 测试

公开仓库单元测试：

```powershell
python -m unittest discover -s tests
```

一次性测试输出、日志和临时对照文件应放进 `tests/_artifacts/<yyyy-mm-dd>-<topic>/`，不要散落在仓库根目录。

## 注意事项

- 默认 HTTP API 后端不做 GUI/CDP 点按；目标朋友圈必须先被 WeFlow 缓存/浏览过，当前不会主动调用有副作用的 `POST /sns/export`。legacy GUI 后端仍依赖 Windows 可见桌面，Agent 触发时通常需要提权运行。
- `WeFlow-*`、`config.toml`、最终 insights 产物都属于本地数据，不应提交进公开仓库。
- 群聊默认经过上下文窗口过滤，保留和用户相关的片段；需要完整语料时可将
  `preprocessing.group_context_window.enabled` 设为 `false`，此时群聊保留全量消息流。私聊始终保留全量消息流。
- 图片可走本地 OCR，微信表情目录会跳过，语音转文字失败默认只记录警告。
- 长期归档采用合并覆盖语义：相同相对路径下，新导出覆盖旧导出，用于避免重复运行产生重复文件。
