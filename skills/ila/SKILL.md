---
name: ila
description: "ILA 迭代闭环管理 — 跨平台通用智能体，管理 AI 技能的自动化迭代闭环。"
version: 1.6.0
author: ILA
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [ILA, Iteration, DevOps, CI/CD]
---

# ILA 迭代闭环管理

ILA 是一个跨平台通用智能体，管理 AI 技能/工具的自动化迭代闭环：**需求分析 → 沙箱开发 → A/B 测试 → 部署验证 → 热切换上线**。

Dashboard 可视化：`ila dashboard-url` 获取地址。

## 查看状态

```bash
ila status
```

## 发起迭代

```bash
ila trigger <object_id> "<requirement>"
```

- `object_id` 格式：`<platform>:<type>:<name>`，如 `hermes:skill:my-skill`
- 返回 `{"status": "started", "task_id": "ila-xxx"}`
- 加 `-y` 可自动批准上线

**📦 默认发布行为**：迭代上线后，产物**自动发布为源平台的能力**（对象 ID 前缀决定发布目标）：
- `workbuddy:skill:xxx` → 发布为 WorkBuddy 技能（`~/.workbuddy/skills/xxx`）
- `workbuddy:expert:xxx` → 发布为 WorkBuddy 专家（`plugins/marketplaces/my-experts/plugins/xxx`）
- `openclaw:skill:xxx` → 发布为 OpenClaw 技能（`~/.openclaw/skills/xxx`）
- `openclaw:agent:xxx` → 发布为 OpenClaw 专家（agentDir）
- `hermes:skill:xxx` → 发布为 Hermes 技能（`~/.hermes/skills/xxx`）

迭代完成后 CLI 会提示「📦 已发布: ... 位置: ...」，在对应平台即可发现并使用新能力。

常见平台示例：

```bash
# Hermes 平台（技能）
ila trigger hermes:skill:my-skill "需求"

# WorkBuddy 平台（技能+专家）
ila trigger workbuddy:skill:my-skill "需求"
ila trigger workbuddy:expert:my-expert "需求"

# OpenClaw 平台（技能+专家+channel）
ila trigger openclaw:skill:my-skill "需求"
ila trigger openclaw:agent:my-agent "需求"
```

## 开发框架配置

ILA 迭代调用 Codex 或 Claude Code 进行开发，首次使用需配置（失败时 CLI 会提示安装命令）：

| 框架 | 安装 | 登录 | 配置方式 |
|------|------|------|----------|
| **codex**（默认） | `npm install -g @openai/codex` 或 `brew install codex` | `codex login` | `config/ila_config.yaml → sandbox.framework: codex` |
| **claude_code** | `npm install -g @anthropic-ai/claude-code` | `claude`（OAuth）或设 `ANTHROPIC_API_KEY` | `sandbox.framework: claude_code` |

模型配置：`sandbox.codex_model`（默认 `deepseek-v4-pro`）。

## 监控进度

快照查询：
```bash
ila watch --once
```

持续轮询（5 秒间隔）：
```bash
ila watch --interval 5
```

## 操作

```bash
ila approve   # 批准热切换
ila skip      # 跳过热切换
```

## 版本与报告

```bash
ila version               # 查看 ILA 版本 (静态 + 运行时注册表版本)
ila version <object_id>   # 查看指定对象的当前版本
ila report --version-list # 列出所有版本迭代报告
ila report --version-id <id>  # 查看指定版本的迭代报告
ila rollback <object_id>  # 回滚到上一版本
ila status                # 查看纳管对象/版本统计
```

## Dashboard URL

```bash
ila dashboard-url  # → {"url": "http://..."}
ila staging-url    # → {"url": "http://..."}
```

提示用户打开 Dashboard 查看可视化的迭代流程和验证模式。

## 列出对象

```bash
ila list --json --platform hermes
ila list --json --platform workbuddy   # WorkBuddy (技能+专家)
ila list --json --all                  # 全部平台
```

## Windows 运维

```powershell
# 启动 Dashboard（Windows 下用 python，不用 python3）
cd C:\Users\JASON\ila
python -m ila.cli dashboard --port 9527 --host 0.0.0.0

# 重启 Dashboard（先查占用端口进程再强杀）
$pids = (Get-NetTCPConnection -LocalPort 9527 -ErrorAction SilentlyContinue).OwningProcess | Sort-Object -Unique
foreach ($p in $pids) { taskkill /F /PID $p }
python -m ila.cli dashboard --port 9527 --host 0.0.0.0

# 查看 Launcher 日志（排查热升级问题，日志可能很大用 tail 式读取）
Get-Content C:\Users\JASON\.ila\launcher.log -Tail 50
```

## 典型交互流程

```
1. terminal("ila list --json --all")
   → 展示可迭代的纳管对象 (hermes:skill / workbuddy:skill / workbuddy:expert / ...)

2. terminal("ila trigger <object_id> '优化描述'")
   → 启动迭代，获取 task_id (WorkBuddy 示例: workbuddy:skill:my-skill / workbuddy:expert:my-expert)

3. terminal("ila dashboard-url")
   → 提示用户打开 Dashboard 实时监控

4. terminal("ila watch --once")
   → 检查进度 (每 30-60 秒查一次)

5. 当 status=pending_approval 时：
   terminal("ila staging-url")
   → 获取 staging_url
   
   **验证方式取决于对象类型：**
   - **含 HTML/Web 页面的对象** (如 minesweeper): staging_url 指向对象实际页面
     (例: http://localhost:9527/staging/skill/minesweeper/minesweeper.html)
     用户打开后直接看到新版本的游戏/功能页面 — **不是 ILA Dashboard**
   - **纯后端 / 无 UI 对象**: staging_url 指向 ILA Dashboard (9528) 验证模式
     (绿色虚线高亮 = 本次修改的模块)

   ⚠️ 注意: Web 对象的 staging URL 使用 9527 端口 (Dashboard 的静态文件路由),
   非 Web 对象使用 9528 端口 (单独的 staging Dashboard 实例)。
   
   用户确认后：
   terminal("ila approve")

6. terminal("ila dashboard-url")
   → 提示用户查看生成的迭代报告

7. 迭代成功后，更新目标对象的 SKILL.md：
   - 将"已知可改进点"中的对应项标记为 ✅ 已完成
   - 如有新功能，补充到文档中（玩法、部署、特性等）
   - 示例：`~~HTML 版无最佳记录持久化~~ ✅ 已完成 (ila-20260731-091851)`
```

## 跨平台部署

ILA 支持 Linux / macOS / Windows，通过 PlatformAdapter 插件体系接入不同 AI 平台。

**Python 要求**：`>= 3.8`（代码不使用 3.10+ 专属语法，兼容 3.8/3.9/3.10/3.11+）

### 新平台接入流程

```bash
# 1. 创建适配器 (参考 src/ila/adapters/workbuddy_adapter.py)
# 2. 在 cli.py 的 init_adapters() 注册
# 3. 配置启用: config.yaml → adapters.<platform>.enabled: true
# 4. 重启 Dashboard
# 5. 验证: ila list --platform <platform>
```

### Windows 部署

**一条命令行安装（推荐）**：

```powershell
# 默认方式（GitHub 直连）
iwr -useb https://raw.githubusercontent.com/YingjieGu/ila/master/install.ps1 | iex
```

```powershell
# 网络不通时，使用代理链接（jsdelivr CDN）
iwr -useb https://cdn.jsdelivr.net/gh/YingjieGu/ila@master/install.ps1 | iex
```

```powershell
# 若 PowerShell 执行策略受限（Set-ExecutionPolicy 被拒绝），先设置再执行：
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope Process -Force; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; iwr -useb https://raw.githubusercontent.com/YingjieGu/ila/master/install.ps1 | iex
```

**手动安装（备用）**：

```powershell
git clone https://github.com/YingjieGu/ila.git C:\ila
cd C:\ila
pip install -e ".[dev,dashboard]"
python -m ila.cli dashboard --port 9527 --host 0.0.0.0
```

> ⚠️ PowerShell 不支持 bash 的 `2>&1 | Out-Null` 语法。见 `references/powershell-pitfalls.md`。

### AI 平台侧安装 ILA Skill

ILA 的 SKILL.md 已随仓库分发（`skills/ila/SKILL.md`），安装后自动部署：

- **Windows**: `install.ps1` 第 5 步自动复制到 Hermes / OpenClaw / WorkBuddy 技能目录
- **Linux/macOS**: `bash scripts/deploy-skill.sh`（可指定平台，如 `bash scripts/deploy-skill.sh hermes workbuddy`）
- **手动**: 将 `skills/ila/SKILL.md` 复制到目标平台技能目录的 `ila/SKILL.md`

平台自动发现后即可对话触发迭代。

## 用户偏好

- **当用户说"教我即可，不用你部署验证"**: 只提供步骤说明和命令，不代为执行。用户自己动手验证。
- **迭代完成后**: 始终检查目标对象的 SKILL.md，将代码中已实现但文档未标记的改进项一并更新（不仅是触发当前迭代的那一项）。

## 常见问题

### 部署验证时 staging URL 打不开对象页面

**症状**: 触发了含 HTML 的 skill 迭代 (如 minesweeper)，进入 pending_approval 后，staging URL 显示的是 ILA Dashboard 管控面板，不是对象自己的 HTML 页面。

**根因**: 旧版 ILA 的 staging_url 始终指向 Dashboard 端口 (9528)，Dashboard 只服务自己的管控面板 HTML，没有提供纳管对象静态文件的路由。

**修复**: v1.1+ 已内置 `/staging/{type}/{name}/{path}` 路由，Dashboard 自动从 staging profile 目录提供纳管对象的静态文件。**注意**: staging profile 名称来自 config `adapters.hermes.staging_profile`，默认值为 `ila-test`（不是 `ila-staging`）。`deploy_to_staging` 检测到 HTML 文件时会自动生成正确的 staging URL。详见 `references/staging-static-serving.md`。

### 修改 ILA 自身代码后需要重启

**症状**: 修改了 ILA 的 `api.py`、`orchestrator.py` 或 adapter 代码后，下次迭代行为没有变化。

**原因**: ILA 是常驻进程，代码修改后旧的 Python 字节码仍在内存中。

**修复**: 重启 ILA Dashboard 进程。使用 Launcher 管理的可以通过 Launcher 重启，或手动 kill 后重启动。

```bash
# 查端口占用的进程
lsof -ti :9527 -sTCP:LISTEN | xargs kill
# 重新启动
ila dashboard --port 9527 --host 0.0.0.0 &
```

### SKILL.md 与实际代码状态不同步

**症状**: 迭代完成后，"已知可改进点"列表中某项实际已实现（代码已有该功能），但 SKILL.md 仍标记为待处理。

**原因**: ILA 沙箱开发有时会连带实现多个改进点（如一次迭代同时加了计时器和最佳记录），但 SKILL.md 只标记了触发本次迭代的那一项。

**修复**: 每次迭代成功后，不仅要标记触发的改进项为已完成，还要检查目标对象的实际代码，将代码中已存在但文档未标记的项也一并更新。（参见典型交互流程第 7 步）
