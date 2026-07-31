# ILA Windows 一键安装脚本
# 用法: powershell -ExecutionPolicy Bypass -File install.ps1

param(
    [string]$InstallDir = "C:\ila",
    [string]$RepoUrl = "https://github.com/YingjieGu/ila.git",
    [string]$Branch = "master",
    [int]$DashboardPort = 9527
)

$ErrorActionPreference = "Stop"
Write-Host "═══ ILA 安装程序 ═══" -ForegroundColor Cyan

# 1. 检查 Python 3.10+
Write-Host "`n[1/5] 检查 Python..." -ForegroundColor Yellow
try {
    $pyVersion = python --version 2>&1
    Write-Host "  $pyVersion" -ForegroundColor Green
    $ver = python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  错误: 需要 Python 3.10+，请先安装 https://www.python.org/downloads/" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "  错误: 未找到 Python，请先安装 https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}

# 2. 检查 git
Write-Host "`n[2/5] 检查 Git..." -ForegroundColor Yellow
try {
    git --version 2>&1 | Out-Null
    Write-Host "  Git OK" -ForegroundColor Green
} catch {
    Write-Host "  错误: 未找到 Git，请先安装 https://git-scm.com/download/win" -ForegroundColor Red
    exit 1
}

# 3. 克隆仓库
Write-Host "`n[3/5] 安装 ILA..." -ForegroundColor Yellow
if (Test-Path $InstallDir) {
    Write-Host "  目录已存在，正在更新..." -ForegroundColor Gray
    Set-Location $InstallDir
    git pull origin $Branch
} else {
    Write-Host "  克隆 $RepoUrl ..." -ForegroundColor Gray
    git clone -b $Branch $RepoUrl $InstallDir
}
Set-Location $InstallDir
pip install -e ".[dev]" 2>&1 | Out-Null
Write-Host "  安装完成" -ForegroundColor Green

# 4. 创建默认配置
Write-Host "`n[4/5] 创建配置..." -ForegroundColor Yellow
$ConfigDir = "$env:USERPROFILE\.ila"
$ConfigFile = "$ConfigDir\config.yaml"
if (-not (Test-Path $ConfigDir)) {
    New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null
}
if (-not (Test-Path $ConfigFile)) {
@"
ila:
  home: $($env:USERPROFILE -replace '\\','/')/.ila
  auto_approve: false

adapters:
  hermes:
    enabled: false
  openclaw:
    enabled: false
  workbuddy:
    enabled: false
  ila:
    enabled: true
    project_root: $($InstallDir -replace '\\','/')

dashboard:
  port: $DashboardPort
  host: 0.0.0.0
  theme: dark

sandbox:
  default_level: tempdir
  framework: codex
"@ | Out-File -FilePath $ConfigFile -Encoding UTF8
    Write-Host "  已创建: $ConfigFile" -ForegroundColor Green
} else {
    Write-Host "  配置文件已存在，跳过" -ForegroundColor Gray
}

# 5. 完成
Write-Host "`n[5/5] 安装完成！" -ForegroundColor Green
Write-Host ""
Write-Host "  启动 Dashboard:" -ForegroundColor Cyan
Write-Host "    cd $InstallDir" -ForegroundColor White
Write-Host "    python -m ila.cli dashboard --port $DashboardPort --host 0.0.0.0" -ForegroundColor White
Write-Host ""
Write-Host "  状态检查:" -ForegroundColor Cyan
Write-Host "    python -m ila.cli status" -ForegroundColor White
Write-Host "    curl http://localhost:$DashboardPort/api/status" -ForegroundColor White
Write-Host ""
Write-Host "  启用平台适配器:" -ForegroundColor Cyan
Write-Host "    编辑 $ConfigFile，将对应平台的 enabled 改为 true" -ForegroundColor White
Write-Host ""
Write-Host "  接入 WorkBuddy:" -ForegroundColor Cyan
Write-Host "    在 WorkBuddy skills 目录创建 ila/SKILL.md，内容参考下面链接" -ForegroundColor White
