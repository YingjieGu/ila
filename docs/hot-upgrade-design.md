# ILA 热升级机制设计文档

> 创建时间：2026-07-28
> 状态：方案 F (ILA Launcher) 已选定，待实施

---

## 1. 问题定义

### 1.1 核心问题

ILA Dashboard 在自升级时，`promote_staging()` 由运行在 9527 的进程自身调用，执行流程为：

```
停止旧版 9527 → 启动新版 9527 → 健康检查 → 停止 9528
```

当此流程由 Dashboard 自身（9527 进程）通过 API 触发时：

1. `_stop_current_dashboard()` —— 虽跳过当前 PID，但 uvicorn worker 进程被杀
2. `_start_dashboard_process(9527)` → 内部先调用 `_kill_port_processes(9527)` —— **不跳过当前 PID**，直接 SIGKILL 当前进程
3. **当前进程死亡，`subprocess.Popen` 没机会执行，新版永远起不来**

一句话总结：**进程不能自己杀死自己再启动替代者。**

### 1.2 扩展需求

热升级机制需要覆盖两种场景：

| 场景 | 谁执行重启 | 谁被重启 | 难点 |
|------|-----------|---------|------|
| **ILA 自升级** | ILA 自己 | ILA 自己 | 执行者 = 被重启者，会杀死自己 |
| **ILA 纳管外部服务** | ILA | 外部服务 | 不同进程，简单 `subprocess` 即可 |

### 1.3 设计约束

| 约束 | 优先级 | 说明 |
|------|--------|------|
| 跨操作系统 | P0 | Linux / macOS / Windows 均可用 |
| 统一机制 | P0 | 自升级和纳管服务热切换用同一套机制 |
| 无外部依赖 | P1 | 不依赖 systemd / atd / 容器编排等 |
| 低侵入性 | P1 | 不需要 root，不修改系统配置 |
| 容器兼容 | P1 | Docker / K8s 环境可直接运行 |
| 可调试 | P2 | 故障时可定位问题 |

---

## 2. 候选方案分析

### 2.1 方案概览

| 方案 | 原理 | 核心依赖 |
|------|------|---------|
| A: Peer Promotion | 委托 9528 staging 进程执行升级 | 需要 9528 存活 |
| B: Fork + Exec | fork 子进程，等父进程死后 exec 新版 | Unix fork() |
| C/D: systemd | 注册 systemd service，systemctl restart | systemd + root |
| E: at 调度 | 用 `at` 调度延迟启动命令 | atd 守护进程 |
| F: ILA Launcher | 独立 Launcher 子进程，命令文件驱动 | 纯 Python（零依赖）|

---

### 2.2 方案 A: Peer Promotion（委托 9528）

**原理**：9527 收到批准请求后，POST 给 9528 的 `/api/admin/promote` 端点，由 9528 执行完整的 `promote_staging()` 流程。9528 是独立进程，杀 9527 不影响自身。

```
用户点"批准上线"
  │
  ▼
9527 收到请求 → POST http://127.0.0.1:9528/api/admin/promote
  │
  ├─→ 9527 返回 "升级已启动" 后自行退出
  │
  ▼
9528 执行: 杀 9527 → 启新 9527 → 健康检查 → 9528 自停
```

**优点**：利用已有 staging 进程，无外部依赖，API 响应正常

**致命缺点**：
- **纳管服务无法复用** —— 外部服务没有 "staging peer"，需要另外一套机制
- 9528 必须存活且可用

---

### 2.3 方案 B: Fork + Exec

**原理**：Unix 经典模式 —— `fork()` 创建子进程副本，父进程退出后子进程变孤儿被 init 收养，然后 `exec()` 替换为新版 dashboard。

```python
pid = os.fork()
if pid == 0:
    # 子进程: 等父进程死 → exec 新版
    os.setsid()
    close_all_fds()
    wait_parent_dead()
    os.execvp('python3', ['python3', '-m', 'ila.cli', 'dashboard', ...])
else:
    # 父进程: 返回 API 响应后退出
    return {"status": "promoting", "child_pid": pid}
```

**优点**：零外部依赖，经典 Unix 模式

**致命缺点**：

| 问题 | 说明 |
|------|------|
| **Windows 不支持** | `os.fork()` 在 Windows 上直接抛 `AttributeError` |
| **Python 线程安全** | fork 只复制调用线程，uvicorn worker 线程在子进程中消失。若消失的线程持有锁，子进程死锁 |
| **文件描述符泄漏** | 子进程继承 uvicorn 的监听 socket，不关则端口不释放 |
| **asyncio 不兼容** | uvicorn event loop 在子进程中处于不确定状态 |
| **调试困难** | 子进程无终端输出，无日志，静默失败 |

**结论**：在 Python + uvicorn（多线程）环境下 fork 极其危险，且不可跨平台。

---

### 2.4 方案 D: systemd Service

#### 2.4.1 原理

将 ILA Dashboard 注册为 systemd 服务，`promote_staging()` 通过 `systemctl restart` 完成。systemd 管理整个生命周期。

```
旧进程                       systemd                       新进程
  │                            │                             │
  │ systemctl restart ila-dash │                             │
  │──────────────────────────►│                             │
  │                            │ SIGTERM → 旧进程            │
  │◄─────────────────────────│                             │
  │ (优雅关闭)                  │                             │
  │                            │ 等旧进程退出                 │
  │                            │ 启动新进程                   │
  │                            │─────────────────────────►  │
  │                            │                             │ 监听 9527
```

#### 2.4.2 root 权限分析

**一次性配置**（创建 unit 文件）需要 root：

```bash
sudo tee /etc/systemd/system/ila-dashboard.service <<'EOF'
[Unit]
Description=ILA Dashboard
[Service]
Type=simple
User=Admin
ExecStart=/usr/bin/python3 -m ila.cli dashboard --port 9527 --host 0.0.0.0
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable ila-dashboard
```

**每次执行** `systemctl restart` 也需要 root。解决方式：

| 办法 | 代价 |
|------|------|
| sudoers 白名单 | 每次调 `sudo` |
| PolKit 规则 | 配置复杂，发行版差异大 |
| 用户级 systemd | 用户退出后服务也死 |

**纳管服务需要持续 root**：ILA 每纳管一个新服务，就要写一个新的 unit 文件 → `systemctl daemon-reload` → 均需 root。这不是一次性成本。

#### 2.4.3 容器兼容性分析

systemd 在容器中运行需要：

```bash
docker run --privileged \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw \
  --cgroupns=host \
  -e container=docker \
  <image> /sbin/init
```

**不兼容原因**：

| 问题 | 说明 |
|------|------|
| `--privileged` | 放弃容器安全隔离 |
| cgroup 挂载路径 | 不同运行时（Docker/Podman/containerd）路径不同 |
| Kubernetes | Pod 设计为单进程，塞 systemd 违反设计意图 |
| 镜像体积 | systemd + 依赖 > 100MB |
| 启动性能 | systemd 初始化比直接跑 Python 慢 5-10 倍 |

#### 2.4.4 综合判定

| 部署场景 | systemd 可用 |
|---------|:----------:|
| 裸机 Linux | ✅ |
| 裸机 macOS | ❌ |
| 容器 Docker | ❌ |
| 容器 K8s | ❌ |
| Windows | ❌ |

**结论：仅 1/5 场景可用。**

---

### 2.5 方案 E: at 调度

**原理**：在杀死自己之前，用 Unix `at` 命令将"启动新版本"调度到 3 秒后执行。

```python
start_cmd = f"cd {project_root} && python3 -m ila.cli dashboard --port 9527 --host 0.0.0.0"
subprocess.run(["at", "now + 3 seconds"], input=start_cmd, text=True)
os._exit(0)
```

**优点**：实现极简（~20 行），依赖标准 Unix 工具

**缺点**：

| 问题 | 说明 |
|------|------|
| Windows 不支持 | `at` 是 Unix 专有 |
| `atd` 可能未装 | 轻量容器镜像默认不带 |
| 固定延迟 | 3 秒硬编码，不够优雅，有服务中断窗口 |
| 失败无通知 | at 任务失败无法自动感知 |

---

### 2.6 方案 F: ILA Launcher（通用进程守护）

#### 2.6.1 架构设计

ILA 启动时 spawn 一个极简的 **Launcher 子进程**。该进程：

- **独立存活**：父进程死后变孤儿 → 被 init(PID 1) 收养 → 继续运行
- **命令驱动**：通过 JSON 命令文件接收重启指令
- **通用执行**：不管目标是 ILA 自己还是纳管服务，流程完全相同

```
┌─────────────────────────────────────────────┐
│              ILA Core Process               │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐ │
│  │Orchestr. │  │Dashboard │  │  Launcher  │ │
│  │          │  │:9527     │  │  Manager   │─┼──→ spawn
│  └──────────┘  └──────────┘  └───────────┘ │
└─────────────────────────────────┬───────────┘
                                  │ subprocess.Popen
                                  │ (start_new_session=True)
                    ┌─────────────▼─────────────┐
                    │    ILA Launcher (独立进程)  │
                    │    PID 独立，ILA 死我不死   │
                    │                            │
                    │  扫描: ~/.ila/commands/    │
                    │                            │
                    │  RESTART <name> <port>     │
                    │    cmd=["python3","-m",...]│
                    │    → kill port → start new │
                    │    → health check          │
                    │    → write result          │
                    └────────────────────────────┘
```

#### 2.6.2 自升级流程

```
用户点 "批准上线"
  │
  ▼
9527 Dashboard API handler
  │
  │  ├─→ 写命令文件: ~/.ila/commands/restart-abc123.json
  │  │     {
  │  │       "action": "restart",
  │  │       "name": "ila-dashboard",
  │  │       "port": 9527,
  │  │       "cmd": ["python3", "-m", "ila.cli", "dashboard",
  │  │                "--port", "9527", "--host", "0.0.0.0"],
  │  │       "cwd": "/home/Admin/myprojects/ila",
  │  │       "staging_port": 9528,
  │  │       "health_check_url": "http://127.0.0.1:9527/api/status",
  │  │       "health_check_timeout": 30
  │  │     }
  │
  ├─→ 返回 API 响应: {"status": "promoting"}
  │
  └─→ 旧进程退出

  ─── 以下由 Launcher 独立执行 ───

  Launcher 检测到命令文件
  │
  ├─ ① kill 9527 上的旧进程 (SIGTERM → 3s → SIGKILL)
  ├─ ② 等端口释放
  ├─ ③ 启动新 dashboard 在 9527
  ├─ ④ 轮询 /api/status 健康检查 (最多 30s)
  ├─ ⑤ kill 9528 staging 进程
  ├─ ⑥ 清理 staging 文件和验证标记
  └─ ⑦ 写结果文件: restart-abc123.result.json
```

#### 2.6.3 纳管服务流程

```
Orchestrator 决定升级 service-x
  │
  ├─→ 写命令文件: restart-def456.json
  │     {
  │       "action": "restart",
  │       "name": "service-x",
  │       "port": 8080,
  │       "cmd": ["java", "-jar", "service-x.jar", "--port", "8080"],
  │       "health_check_url": "http://127.0.0.1:8080/health"
  │     }
  │
  ├─→ 轮询结果文件 (最多 30s)
  │
  └─→ 收到 {"status": "success"} → 继续

  流程与自升级完全一致
```

#### 2.6.4 跨平台适配

```python
import platform

class PlatformAdapter:
    """封装平台差异."""

    @staticmethod
    def kill_port(port: int) -> None:
        if platform.system() == "Windows":
            # netstat -ano | findstr :{port} → taskkill /F /PID {pid}
            ...
        else:
            # lsof -ti :{port} | xargs kill -9
            ...

    @staticmethod
    def spawn_process(cmd: list[str]) -> subprocess.Popen:
        kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if platform.system() == "Windows":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return subprocess.Popen(cmd, **kwargs)
```

#### 2.6.5 版本正确性保证

**核心问题**：9527 重启后，运行的到底是旧代码还是新代码？

**答案**：新代码。由 ILA 迭代闭环的**调用时序**天然保证。

##### 时序分析

ILA 迭代闭环的关键步骤执行顺序：

```
run_iteration():
  Phase 1: 沙箱开发 (Codex)     → 沙箱中生成新代码
  Phase 2: A/B 对比测试
  Phase 3: deploy_to_staging()  ← ★ 决定性步骤
           ├─ ① 备份 src/ila/ 旧文件到 /tmp/
           ├─ ② 复制沙箱文件 → src/ila/     ← 项目文件已被替换！
           └─ ③ 启动新 ILA 在 9528

  Phase 4: 部署验证
           └─ 用户在 9528 上查看验证高亮

  Phase 5: promote_staging() 或用户点击"批准上线"
           └─ Launcher 执行:
              kill 9527 → python3 -m ila.cli dashboard --port 9527
                                           ↑
                              从 src/ila/ 加载 → Step ② 已替换的新代码
```

**`deploy_to_staging()` 在 `promote_staging()` 之前执行。** 当 Launcher 启动新的 9527 进程时，`src/ila/` 中的文件已经是沙箱产出的新版本。`python3 -m ila.cli` 从项目目录导入 `ila` 包，加载的就是新代码。

##### cwd 显式加固

虽然默认情况下 Launcher 子进程会继承父进程的工作目录（通常是项目根目录），但为了防御潜在的路径问题，`promote_staging()` 在发送重启命令时**显式指定 `cwd`**：

```python
launcher.send_restart(
    name="ila-dashboard",
    port=self.dashboard_port,
    cmd=["python3", "-m", "ila.cli", "dashboard",
         "--port", str(self.dashboard_port), "--host", "0.0.0.0"],
    cwd=self.project_root,  # ← 强制从项目根目录启动
    ...
)
```

这条 `cwd` 经以下链路传递到最终的 `subprocess.Popen`：

```
promote_staging()
  └─ launcher.send_restart(cwd=...)
       └─ 写入命令 JSON: {"cwd": "/home/Admin/myprojects/ila", ...}
            └─ Launcher._execute_command()
                 └─ spawn_detached(cmd, cwd=cmd.get("cwd"))
                      └─ subprocess.Popen(cmd, cwd=cwd)
```

**即使 Launcher 将来被放在其他目录执行，新 dashboard 进程也一定从 `self.project_root` 启动。**

##### 唯一例外：Launcher 自身

Launcher 本身（`ila.launcher`、`ila.launcher_platform` 等模块）如果在本轮迭代中被修改，**不会**通过热重启生效：

- Launcher 是独立常驻进程，不会重启自己
- 只有 Dashboard **下次冷启动**（`python3 -m ila.cli dashboard`）时才会加载新版 Launcher
- 影响极小：Launcher 是极简代码（~200 行），功能稳定，几乎不需要修改

##### 命令文件格式（含 cwd）

```json
{
  "action": "restart",
  "name": "ila-dashboard",
  "port": 9527,
  "cmd": ["python3", "-m", "ila.cli", "dashboard", "--port", "9527", "--host", "0.0.0.0"],
  "cwd": "/home/Admin/myprojects/ila",
  "health_check_url": "http://127.0.0.1:9527/api/status",
  "health_check_timeout": 30,
  "staging_port": 9528,
  "cleanup": {
    "verification_mode": true,
    "staging_info_file": "~/.ila/staging/xxx.json"
  }
}
```

---

## 3. 方案对比矩阵

### 3.1 核心维度

| 维度 | A:Peer | B:Fork | D:systemd | E:at | **F:Launcher** |
|------|:------:|:------:|:---------:|:----:|:-------------:|
| 自升级 | ✅ | ⚠️ 危险 | ✅ | ✅ | ✅ |
| 纳管服务 | ❌ | ✅ | ✅ | ✅ | ✅ |
| 统一机制 | ❌ | ✅ | ✅ | ✅ | ✅ |
| 跨 OS | ✅ | ❌ Win | ❌ Linux only | ❌ Win | ✅ |
| 容器兼容 | ✅ | ✅ | ❌ | ⚠️ | ✅ |
| 零外部依赖 | ✅ | ✅ | ❌ | ❌ | ✅ |
| 不需 root | ✅ | ✅ | ❌ | ✅ | ✅ |
| 可靠性 | ⭐⭐ | ⭐⭐ | ⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐ |
| 可调试性 | ⭐⭐ | ⭐ | ⭐⭐⭐ | ⭐⭐ | ⭐⭐⭐ |
| 侵入性 | 低 | 低 | **高** | 低 | 低 |
| 实现量 | ~50行 | ~100行 | ~30行 | ~20行 | ~200行 |

### 3.2 部署场景覆盖

| 部署场景 | A | B | D | E | **F** |
|---------|:--:|:--:|:--:|:--:|:--:|
| 裸机 Linux | ✅ | ✅ | ✅ | ✅ | ✅ |
| 裸机 macOS | ✅ | ✅ | ❌ | ✅ | ✅ |
| 裸机 Windows | ✅ | ❌ | ❌ | ❌ | ✅ |
| Docker 容器 | ✅ | ✅ | ❌ | ⚠️ | ✅ |
| Kubernetes | ✅ | ✅ | ❌ | ❌ | ✅ |

---

## 4. 结论

**方案 F（ILA Launcher — 通用进程守护）是唯一同时满足所有约束的方案。**

### 4.1 核心优势

| 优势 | 说明 |
|------|------|
| 🖥️ 跨操作系统 | Linux / macOS / Windows 通过平台适配层统一接口 |
| 🔄 统一机制 | 自升级和纳管服务走同一套命令文件协议 |
| 🪶 零依赖 | ~200 行 Python，不依赖 systemd/atd/容器编排 |
| 🛡️ 进程隔离 | Launcher 与 ILA 进程独立，ILA 崩溃不影响 Launcher |
| 🔧 可调试 | 命令文件和结果文件落在磁盘上，出问题直接 `cat` 查看 |
| 🔌 低侵入 | 不需要 root、不需要注册系统服务 |
| 📦 容器友好 | Docker / K8s 直接可用，无需 `--privileged` |

### 4.2 为什么其他方案被淘汰

| 方案 | 淘汰原因 |
|------|---------|
| A: Peer Promotion | 纳管服务无法复用，需要两套机制 |
| B: Fork + Exec | Windows 不支持；Python 多线程下 fork 危险 |
| D: systemd | Linux 专有；需要 root；容器不可用；纳管服务需要持续 root |
| E: at 调度 | Windows 不支持；依赖 atd；固定延迟不优雅；无失败通知 |

---

## 5. 实施计划

### 5.1 文件结构

```
ila/
├── src/ila/
│   ├── launcher.py          # Launcher 核心（命令扫描 + 执行引擎）
│   ├── launcher_manager.py  # ILA 侧管理器（spawn/stop Launcher）
│   └── launcher_platform.py # 平台适配层（Linux/macOS/Windows）
├── tests/
│   └── test_launcher.py     # 单元测试
└── docs/
    └── hot-upgrade-design.md # 本文档
```

### 5.2 改造点

| 组件 | 改动 | 说明 |
|------|------|------|
| `cli.py` | ILA 启动时 spawn Launcher | `init_launcher()` |
| `ila_self_adapter.py` | `promote_staging()` 改写命令文件 | 不再自己执行，委托给 Launcher |
| `orchestrator.py` | 纳管服务热切换走 Launcher | 新增 `_delegate_restart()` |
| `launcher.py` | 新增 | 核心实现 |

### 5.3 状态机

```
Launcher 状态:
  IDLE → 检测命令 → EXECUTING → (success|error) → IDLE

命令文件生命周期:
  restart-{id}.json        ← 命令（由 ILA 写入）
  restart-{id}.result.json ← 结果（由 Launcher 写入）
  restart-{id}.log         ← 日志（由 Launcher 写入）

ILA 侧轮询:
  写命令 → 轮询 .result.json (最多 30s) → 读结果 → 返回
```

关键设计                                                                                                                                                                                
                                                                                                                                                                                            
      ILA Dashboard (9527)              ILA Launcher (独立 PID)                                                                                                                             
          │                                    │                                                                                                                                            
          │ promote_staging()                  │                                                                                                                                            
          ├─ 写 restart-{id}.json ──────────→ 扫描 ~/.ila/commands/                                                                                                                         
          ├─ 返回 {"status":"promoting"}       │                                                                                                                                            
          └─ 进程退出                          ├─ kill 旧 9527                                                                                                                              
                                               ├─ 启动新 9527                                                                                                                               
                                               ├─ 健康检查                                                                                                                                  
                                               ├─ kill 9528                                                                                                                                 
                                               └─ 写 restart-{id}.result.json 