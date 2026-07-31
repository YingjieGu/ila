# ILA 自升级架构设计

## 核心问题

**ILA 自身作为纳管对象，通过自身的闭环流程进行升级。**

与其他对象（如扫雷技能）不同，ILA 自升级面临三个独特挑战：

1. **自举问题** — 升级自身的代码，同时自身要运行升级流程
2. **双实例并行** — 旧版和新版 ILA 需要同时运行才能做 A/B 对比
3. **热切换零停机** — 从旧版切换到新版不能中断正在服务的 API

---

## 一、关键设计决策

### 决策1: 当前 ILA 负责执行迭代，升级的是新 ILA 进程

```
ILA v1 (当前进程, port 9527)  ← 执行迭代闭环
  ├─ 分析 → 开发 → 测试 → 部署 → 切换
  └─ 升级目标是: ILA v2 (新进程, port 9528)
```

- 不存在"鸡生蛋"循环 — 当前版本始终保持运行，管理升级流程
- 新版本是独立进程，与当前版本隔离
- 回滚只需停止新进程、保持旧进程

### 决策2: 新增 `IlaSelfAdapter`，不修改现有适配器

- 平台 ID: `"ila"`
- 对象 ID: `"ila:agent:core"`
- 路径: `/home/Admin/myprojects/ila/src/ila/`
- 与 HermesAdapter、OpenClawAdapter 平级，注册在 AdapterRegistry 中

### 决策3: A/B 测试通过 HTTP API 对比，而非 LLM 调用

- 旧版 ILA: `http://localhost:9527/api/status`
- 新版 ILA: `http://localhost:9528/api/status`
- 测试用例: 调用相同 API 端点，对比 JSON 响应
- 加速方式: 使用文件检查 + HTTP curl，不调用 LLM

---

## 二、IlaSelfAdapter 实现

### 2.1 对象发现

```python
class IlaSelfAdapter(PlatformAdapter):
    def platform_id(self) -> str:
        return "ila"

    def discover_objects(self) -> list[ManagedObject]:
        return [
            ManagedObject(
                object_id="ila:agent:core",
                platform="ila",
                object_type="agent",
                name="ila-core",
                path=os.path.expanduser("~/myprojects/ila/src/ila"),
                current_version=self._read_version(),
                metadata={
                    "project_root": "~/myprojects/ila",
                    "dashboard_port": 9527,
                    "protected_files": [
                        "registry.db", "ila_config.yaml", 
                        ".git", "__pycache__", "venv"
                    ],
                },
            )
        ]
```

### 2.2 快照创建（排除受保护文件）

```python
def create_snapshot(self, obj: ManagedObject) -> str:
    """创建源码快照，排除 .git/venv/__pycache__/registry.db/ila_config.yaml"""
    snapshot_dir = "~/.ila/snapshots/self"
    os.makedirs(snapshot_dir, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    snapshot_path = f"{snapshot_dir}/ila-core-{timestamp}.tar.gz"

    project_root = os.path.expanduser("~/myprojects/ila")
    with tarfile.open(snapshot_path, "w:gz") as tar:
        # 仅打包 src/ 和 config/ 目录
        for dirname in ["src/ila", "config", "tests"]:
            path = os.path.join(project_root, dirname)
            if os.path.exists(path):
                tar.add(path, arcname=dirname,
                        filter=lambda x: None if self._is_excluded(x.name) else x)
    return snapshot_path

def _is_excluded(self, name: str) -> bool:
    excludes = ["__pycache__", ".git", "venv", "node_modules",
                ".pytest_cache", ".egg-info", "registry.db"]
    return any(e in name for e in excludes)
```

### 2.3 部署到 Staging（启动新进程）

```python
def deploy_to_staging(self, obj: ManagedObject, sandbox_path: str) -> str:
    """部署新版本到 staging 并启动新 ILA 进程"""
    staging_id = f"ila-staging-{int(time.time())}"

    # 1. 复制沙箱文件到项目目录
    project_root = os.path.expanduser("~/myprojects/ila")
    src_dir = os.path.join(project_root, "src", "ila")
    config_dir = os.path.join(project_root, "config")

    # 备份旧文件
    backup_dir = f"/tmp/ila-staging-backup-{int(time.time())}"
    shutil.copytree(src_dir, os.path.join(backup_dir, "src"))
    shutil.copytree(config_dir, os.path.join(backup_dir, "config"))

    # 复制新文件
    for entry in os.scandir(sandbox_path):
        if entry.name.startswith((".", "__pycache__")):
            continue
        dest = os.path.join(src_dir, entry.name)
        if entry.is_dir():
            if os.path.exists(dest):
                shutil.rmtree(dest)
            shutil.copytree(entry.path, dest)
        else:
            shutil.copy2(entry.path, dest)

    # 2. 启动新 ILA dashboard 进程 (port 9528)
    new_port = 9528
    env = os.environ.copy()
    env["ILA_DASHBOARD_PORT"] = str(new_port)

    process = subprocess.Popen(
        ["python3", "-m", "ila.cli", "dashboard"],
        cwd=project_root,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # 3. 等待健康检查通过
    for i in range(30):  # 最多等 30s
        time.sleep(1)
        try:
            r = requests.get(f"http://localhost:{new_port}/api/status", timeout=2)
            if r.status_code == 200:
                break
        except:
            pass

    # 保存 staging 信息
    staging_info = {
        "staging_id": staging_id,
        "port": new_port,
        "pid": process.pid,
        "backup_dir": backup_dir,
        "sandbox_path": sandbox_path,
    }
    _save_staging_info(staging_id, staging_info)

    return staging_id
```

### 2.4 A/B 对比测试调用

```python
def invoke_object(self, obj: ManagedObject, test_input: dict) -> dict:
    """调用当前 ILA (port 9527) 的 API"""
    endpoint = test_input.get("endpoint", "/api/status")
    try:
        r = requests.get(f"http://localhost:9527{endpoint}", timeout=10)
        return {"output": r.text, "exit_code": 0, "error": ""}
    except Exception as e:
        return {"output": "", "exit_code": 1, "error": str(e)}

def invoke_staging(self, staging_id: str, test_input: dict) -> dict:
    """调用 staging ILA (port 9528) 的 API"""
    info = _load_staging_info(staging_id)
    port = info["port"]
    endpoint = test_input.get("endpoint", "/api/status")
    try:
        r = requests.get(f"http://localhost:{port}{endpoint}", timeout=10)
        return {"output": r.text, "exit_code": 0, "error": ""}
    except Exception as e:
        return {"output": "", "exit_code": 1, "error": str(e)}
```

### 2.5 热切换（零停机）

```python
def hot_swap(self, obj: ManagedObject, sandbox_path: str) -> dict:
    """热切换: 保持旧进程运行 → 确认新进程健康 → 切换流量 → 停止旧进程"""
    # 1. 创建快照
    snapshot = self.create_snapshot(obj)

    # 2. 部署新文件到 src/
    staging_id = self.deploy_to_staging(obj, sandbox_path)
    info = _load_staging_info(staging_id)
    new_port = info["port"]
    new_pid = info["pid"]

    # 3. 健康检查新进程
    if not self._check_health(new_port):
        # 失败: 停止新进程, 恢复旧文件
        os.kill(new_pid, signal.SIGTERM)
        self._restore_from_backup(info["backup_dir"])
        return {"status": "rolled_back", "reason": "新进程健康检查失败", "snapshot": snapshot}

    # 4. 切换流量: iptables 重定向 9527 → 9528
    # 这样所有连接 9527 的请求透明地转到新进程
    subprocess.run([
        "iptables", "-t", "nat", "-A", "PREROUTING",
        "-p", "tcp", "--dport", "9527",
        "-j", "REDIRECT", "--to-port", str(new_port)
    ], check=False)  # 可能失败（无 root）

    # 替代方案: 如果没有 root, 用 socat 做代理
    # 将旧进程停止, 用 socat 监听 9527 → 转发到 9528
    if not self._has_root():
        # 停止旧进程
        self._stop_current_dashboard()
        # 启动 socat 代理
        subprocess.Popen(["socat", f"TCP-LISTEN:9527,reuseaddr,fork",
                         f"TCP:localhost:{new_port}"])

    # 5. 停止旧进程
    self._stop_current_dashboard()

    # 6. 清理 backup
    shutil.rmtree(info["backup_dir"], ignore_errors=True)

    return {"status": "success", "snapshot": snapshot}
```

### 2.6 健康检查

```python
def health_check(self, obj: ManagedObject) -> bool:
    """检查 ILA dashboard API 是否正常响应"""
    for port in [9527, 9528]:
        try:
            r = requests.get(f"http://localhost:{port}/api/status", timeout=3)
            if r.status_code == 200:
                return True
        except:
            pass
    return False

def reload(self, obj: ManagedObject) -> bool:
    """ILA 重载 = 重启 dashboard 进程"""
    # 重启由 hot_swap 管理, 这里做文件完整性验证
    src_dir = os.path.expanduser("~/myprojects/ila/src/ila")
    required = ["__init__.py", "cli.py", "core/orchestrator.py", 
                "adapters/__init__.py", "adapters/base.py"]
    for f in required:
        if not os.path.exists(os.path.join(src_dir, f)):
            return False
    return True
```

---

## 三、测试用例设计

```python
def generate_self_test_cases() -> list[dict]:
    """为 ILA 自升级生成的 A/B 测试用例"""
    return [
        # 1. API 健康检查
        {"id": "api-status", "type": "functional",
         "input": {"endpoint": "/api/status"},
         "expected": '"platforms"'},  # 响应应包含 platforms 字段

        # 2. 对象列表
        {"id": "api-objects", "type": "functional",
         "input": {"endpoint": "/api/objects"},
         "expected": '"object_id"'},

        # 3. 版本列表
        {"id": "api-versions", "type": "functional",
         "input": {"endpoint": "/api/versions"},
         "expected": '"version_id"'},

        # 4. pytest 测试套件
        {"id": "pytest-self", "type": "regression",
         "input": {"run_test": "pytest tests/"},
         "expected": "passed"},

        # 5. 能力纳管数
        {"id": "api-discover", "type": "regression",
         "input": {"endpoint": "/api/objects", "count_field": "objects"},
         "expected": ""},  # 只比对 exit_code
    ]
```

---

## 四、安全保护机制

### 4.1 受保护文件列表

沙箱复制和部署时必须排除以下文件，确保自升级不会破坏关键数据：

| 文件/目录 | 原因 | 保护方式 |
|-----------|------|---------|
| `registry.db` | 版本注册表数据 | 不复制到沙箱 |
| `ila_config.yaml` (runtime) | 运行时配置 | 不覆盖 |
| `.git/` | 版本控制 | 不复制 |
| `__pycache__/` | 缓存 | 不复制 |
| `venv/` | 虚拟环境 | 不复制 |
| `node_modules/` | JS 依赖 | 不复制 |
| `logs/` | 日志 | 不复制 |

### 4.2 回滚策略

```
热切换流程中:
  1. 旧进程保持运行 (port 9527) ← 回滚保底
  2. 新进程启动 (port 9528) ← 测试目标
  3. 健康检查通过 → 切换流量 → 停止旧进程
  4. 健康检查失败 → 停止新进程, 保持旧进程 ← 零风险

回滚命令:
  ila rollback ila:agent:core
  → 从快照恢复 src/ila/ 下的文件
  → 重启 dashboard 进程
  → 如果旧进程仍在运行, 切换回旧进程
```

### 4.3 鸡生蛋自举问题

```
ILA v1 (当前进程, port 9527)
  └─ 执行迭代: 分析 → 开发 → 测试 → 部署 → 切换
       └─ 目标: ILA v2 (新进程, port 9528)
            └─ 由 ILA v1 的当前代码创建和验证

不存在自举循环:
  - ILA v1 的代码运行整个迭代流程
  - ILA v2 只是被操作的目标对象
  - 即使 ILA v2 修改了迭代逻辑, 也是 ILA v1 在执行
  - 切换后, ILA v2 成为新的当前版本, 可管理下一次升级
```

---

## 五、与现有系统集成

### 5.1 注册表注册

在 `cli.py` 的 `init_adapters()` 中新增:

```python
def init_adapters(config):
    # ... 现有 Hermes/OpenClaw 初始化 ...

    # ILA 自适配器 (始终启用)
    from ila.adapters.ila_self_adapter import IlaSelfAdapter
    AdapterRegistry.register(IlaSelfAdapter())
```

### 5.2 发现

```bash
ila discover --platform ila
# → 发现 1 个纳管对象:
#   ila:agent:core  agent  v1.0.0  ~/myprojects/ila/src/ila/
```

### 5.3 触发迭代

```bash
ila iterate ila:agent:core \
  --requirement "让 ILA 支持自动发现 platform-agnostic 对象的 MCP 配置" \
  --auto-approve
```

### 5.4 配置变更

```yaml
# ila_config.yaml
adapters:
  ila:
    enabled: true
    dashboard_port: 9527
    staging_port: 9528
    project_root: ~/myprojects/ila
    source_dir: src/ila
    protected_files:
      - registry.db
      - ila_config.yaml
      - .git
      - __pycache__
      - venv
    rollout:
      health_check_retries: 30
      health_check_interval: 1
      traffic_switch_method: iptables  # 或 socat / proxy
      keep_old_process: true           # 回滚保底
```

---

## 六、实施路线图

```
Phase 1: 基础设施 (1-2天)
  ├─ 创建 IlaSelfAdapter 类
  ├─ 实现 discover_objects / create_snapshot / restore_snapshot
  ├─ 实现 get_object_files / validate_compatibility
  └─ 注册到 AdapterRegistry, 验证 discover 命令

Phase 2: Staging 部署 (1-2天)
  ├─ 实现 deploy_to_staging: 复制文件 + 启动新进程
  ├─ 实现 invoke_object / invoke_staging: HTTP API 调用
  ├─ 实现 health_check: API 健康检查
  └─ 验证 A/B 测试流程

Phase 3: 热切换上线 (1-2天)
  ├─ 实现 hot_swap: 部署 → 健康检查 → 切换 → 停止旧进程
  ├─ 实现 reload: 文件完整性验证
  ├─ 实现流量切换 (iptables / socat 两种方案)
  └─ 验证完整闭环: 分析 → 开发 → 测试 → 部署 → 切换

Phase 4: 安全加固 (1天)
  ├─ 受保护文件列表校验
  ├─ 快照永久保留机制
  ├─ 回滚测试 (模拟升级失败)
  └─ 文档 + 操作指南
```

---

## 七、与现有系统的差异对比

| 方面 | 扫雷技能 (HermesAdapter) | ILA 自升级 (IlaSelfAdapter) |
|------|------------------------|---------------------------|
| 对象类型 | skill | agent |
| 纳管路径 | `~/.hermes/skills/games/minesweeper/` | `~/myprojects/ila/src/ila/` |
| 部署方式 | 替换文件, 新 session 自动加载 | 替换文件 + 启动新进程 |
| 重载机制 | 隐式 (Hermes 自动 reload) | 显式 (进程管理) |
| 健康检查 | 文件完整性 | API 响应 + 进程存活 |
| 回滚方式 | 恢复快照文件 | 停止新进程, 恢复旧进程 |
| 鸡生蛋问题 | 不存在 | 当前版本管理新版本 |
| A/B 测试 | 文件对比 (快) | HTTP API 对比 (中等) |
| 流量切换 | 无需 (文件级) | iptables/socat/proxy |