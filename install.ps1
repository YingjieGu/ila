# ILA Windows install script
# Usage: powershell -ExecutionPolicy Bypass -File install.ps1

param(
    [string]$InstallDir = "C:\Users\JASON\ila",
    [string]$RepoUrl = "https://github.com/YingjieGu/ila.git",
    [string]$Branch = "master",
    [int]$DashboardPort = 9527
)

# UTF-8 encoding support
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8

$ErrorActionPreference = "Stop"
Write-Host "=== ILA Installer ===" -ForegroundColor Cyan

# 1. Check Python 3.8+
Write-Host "`n[1/5] Checking Python..." -ForegroundColor Yellow
try {
    $pyVersion = python --version 2>&1
    Write-Host "  $pyVersion" -ForegroundColor Green
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,8) else 1)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ERROR: Python 3.8+ required. Install from https://www.python.org/downloads/" -ForegroundColor Red
        exit 1
    }
} catch {
    Write-Host "  ERROR: Python not found. Install from https://www.python.org/downloads/" -ForegroundColor Red
    exit 1
}

# 2. Check Git
Write-Host "`n[2/5] Checking Git..." -ForegroundColor Yellow
try {
    git --version *>$null
    Write-Host "  Git OK" -ForegroundColor Green
} catch {
    Write-Host "  ERROR: Git not found. Install from https://git-scm.com/download/win" -ForegroundColor Red
    exit 1
}

# 3. Clone & install
Write-Host "`n[3/5] Installing ILA..." -ForegroundColor Yellow
if (Test-Path $InstallDir) {
    Write-Host "  Directory exists, updating..." -ForegroundColor Gray
    Set-Location $InstallDir
    git pull origin $Branch
} else {
    Write-Host "  Cloning $RepoUrl ..." -ForegroundColor Gray
    git clone -b $Branch $RepoUrl $InstallDir
}
Set-Location $InstallDir
pip install -e ".[dev,dashboard]"
Write-Host "  Install complete" -ForegroundColor Green

# 4. Create default config
Write-Host "`n[4/5] Creating config..." -ForegroundColor Yellow
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
    enabled: true
    workbuddy_home: $($env:USERPROFILE -replace '\\','/')/.workbuddy
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
    Write-Host "  Created: $ConfigFile" -ForegroundColor Green
} else {
    Write-Host "  Config exists, skipped" -ForegroundColor Gray
}

# 5. Deploy ILA skill to platforms
Write-Host "`n[5/6] Deploying ILA skill to platforms..." -ForegroundColor Yellow
$SkillSource = Join-Path $InstallDir "skills\ila\SKILL.md"
if (Test-Path $SkillSource) {
    # Hermes
    $hermesSkillDir = "$env:USERPROFILE\.hermes\skills\ila"
    if (-not (Test-Path $hermesSkillDir)) { New-Item -ItemType Directory -Path $hermesSkillDir -Force | Out-Null }
    Copy-Item $SkillSource "$hermesSkillDir\SKILL.md" -Force
    Write-Host "  ✓ Hermes:    $hermesSkillDir\SKILL.md" -ForegroundColor Green

    # OpenClaw
    $openclawSkillDir = "$env:USERPROFILE\.openclaw\skills\ila"
    if (-not (Test-Path $openclawSkillDir)) { New-Item -ItemType Directory -Path $openclawSkillDir -Force | Out-Null }
    Copy-Item $SkillSource "$openclawSkillDir\SKILL.md" -Force
    Write-Host "  ✓ OpenClaw:  $openclawSkillDir\SKILL.md" -ForegroundColor Green

    # WorkBuddy
    $workbuddySkillDir = "$env:USERPROFILE\.workbuddy\skills\ila"
    if (-not (Test-Path $workbuddySkillDir)) { New-Item -ItemType Directory -Path $workbuddySkillDir -Force | Out-Null }
    Copy-Item $SkillSource "$workbuddySkillDir\SKILL.md" -Force
    Write-Host "  ✓ WorkBuddy: $workbuddySkillDir\SKILL.md" -ForegroundColor Green

    # OpenCode (如存在 skills 目录)
    $opencodeSkillDir = "$env:USERPROFILE\.opencode\skills\ila"
    if (Test-Path "$env:USERPROFILE\.opencode") {
        if (-not (Test-Path $opencodeSkillDir)) { New-Item -ItemType Directory -Path $opencodeSkillDir -Force | Out-Null }
        Copy-Item $SkillSource "$opencodeSkillDir\SKILL.md" -Force
        Write-Host "  ✓ OpenCode:  $opencodeSkillDir\SKILL.md" -ForegroundColor Green
    } else {
        Write-Host "  - OpenCode:  未安装，跳过" -ForegroundColor Gray
    }
} else {
    Write-Host "  ⚠️  未找到 $SkillSource，跳过 skill 部署" -ForegroundColor Yellow
}

# 6. Done
Write-Host "`n[6/6] Install complete!" -ForegroundColor Green
Write-Host ""
Write-Host "  Start Dashboard:" -ForegroundColor Cyan
Write-Host "    cd $InstallDir" -ForegroundColor White
Write-Host "    python -m ila.cli dashboard --port $DashboardPort --host 0.0.0.0" -ForegroundColor White
Write-Host ""
Write-Host "  Check status:" -ForegroundColor Cyan
Write-Host "    python -m ila.cli status" -ForegroundColor White
Write-Host "    curl http://localhost:$DashboardPort/api/status" -ForegroundColor White
Write-Host ""
Write-Host "  Enable platform adapters:" -ForegroundColor Cyan
Write-Host "    Edit $ConfigFile, set enabled: true for your platform" -ForegroundColor White
Write-Host ""
Write-Host "  ILA skill 已自动部署到 Hermes / OpenClaw / WorkBuddy 技能目录" -ForegroundColor Cyan
