# ILA: Iteration Loop Agent

平台无关的敏捷迭代闭环智能体，通过 Codex 等开发框架的沙箱能力，对任意能力纳管平台（Hermes、OpenClaw 等）所管辖的智能体、Skill、MCP 等服务进行开发改造、自动测试、部署验证、热切换上线全流程自动化。

## 安装

```bash
cd ila
pip install -e ".[dev]"
```

## 使用

```bash
# 发现平台纳管对象
ila discover --platform hermes
ila discover --all

# 执行迭代闭环
ila run hermes:skill:my-skill "修复中文编码bug"

# 仅测试
ila test hermes:skill:my-skill /tmp/sandbox-xxx

# 仅热切换
ila swap hermes:skill:my-skill /tmp/sandbox-xxx

# 回滚
ila rollback hermes:skill:my-skill

# 查看状态
ila status

# 自迭代
ila self-improve "优化测试速度"
```

## 架构

```
┌──────────────────────────────────────────┐
│          ILA 核心迭代引擎                  │
│  (Analyzer → Developer → Tester →        │
│   Deployer → Switcher → Roller)          │
├──────────────────────────────────────────┤
│          平台适配器层                      │
│  HermesAdapter | OpenClawAdapter | ...   │
├──────────────────────────────────────────┤
│          目标平台                          │
│  Hermes | OpenClaw | Custom              │
└──────────────────────────────────────────┘
```
