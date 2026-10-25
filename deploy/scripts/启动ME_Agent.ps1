# ============================================================
# ME_Agent Windows 启动脚本(PowerShell 版)
# 位置: deploy\scripts\ ; 仓库根 = 上两级目录
# 用法:
#   .\启动ME_Agent.ps1                 # 后台静默启动(默认 8000 端口), PID 写入 logs\agent.pid
#   .\启动ME_Agent.ps1 -Port 8080     # 自定义端口
#   .\启动ME_Agent.ps1 -Foreground    # 前台运行(日志直接刷屏, Ctrl+C 停止)
#   .\启动ME_Agent.ps1 -ExtraArgs @('--no-engine')   # 其余参数原样透传 python -m app
# 前置: 项目根已有 .venv 且装好 requirements.txt
# 停止: taskkill /PID <logs\agent.pid 里的数字> /F
# ============================================================
# NOTE: comments kept ASCII-safe; messages are UTF-8 (file saved with BOM).
param(
    [int]$Port = 0,
    [switch]$Foreground,
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = 'Stop'

# Force UTF-8 mode for the child Python (fixes GBK locale issues on Chinese
# Windows: pip auto_decode / config files / state persistence).
# pip reads requirements.txt with locale codec when no BOM present.
$env:PYTHONUTF8 = '1'

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $Root

$VenvPy = Join-Path $Root '.venv\Scripts\python.exe'
$Req = Join-Path $Root 'requirements.txt'

function Fail([string]$msg, [string[]]$fixes) {
    Write-Host ""
    Write-Host "[ME_Agent] 错误: $msg" -ForegroundColor Red
    Write-Host ""
    Write-Host "修复指引(在项目根目录执行):" -ForegroundColor Yellow
    foreach ($f in $fixes) { Write-Host "  $f" }
    Write-Host ""
    Read-Host "按回车键退出"
    exit 1
}

# ---- [1/2] .venv 存在性检查 ----
if (-not (Test-Path $VenvPy)) {
    Fail "虚拟环境不存在: $Root\.venv" @(
        'python -m venv .venv',
        '.venv\Scripts\pip.exe install -r requirements.txt'
    )
}

# ---- [2/2] 关键依赖导入检查 ----
# NOTE: python_multipart 是 pip 包 python-multipart 的导入名(FastAPI 表单/文件上传路由必需)。
# NOTE: tzdata 为 zoneinfo 提供 IANA 时区库(Windows 无系统时区库, Linux/macOS 系统自带);
#       后面的 ZoneInfo('Asia/Shanghai') 即 37 实机崩溃点的功能性探测。
& $VenvPy -c "import fastapi, uvicorn, httpx, yaml, websockets, pydantic, python_multipart, tzdata; from zoneinfo import ZoneInfo; ZoneInfo('Asia/Shanghai')" 2>$null
if ($LASTEXITCODE -ne 0) {
    Fail "关键依赖缺失(fastapi/uvicorn/httpx/yaml/websockets/pydantic/python-multipart/tzdata)" @(
        '.venv\Scripts\pip.exe install -r requirements.txt'
    )
}

# ---- 组装参数并拉起 ----
$pyArgs = @('-m', 'app')
if ($Port -gt 0) { $pyArgs += @('--port', "$Port") }
if ($ExtraArgs.Count -gt 0) { $pyArgs += $ExtraArgs }

if ($Foreground) {
    Write-Host "[ME_Agent] 检查通过, 前台启动(Python $($pyArgs -join ' '))..." -ForegroundColor Green
    & $VenvPy @pyArgs
    exit $LASTEXITCODE
}

$LogDir = Join-Path $Root 'logs'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$OutLog = Join-Path $LogDir 'agent.log'
$ErrLog = Join-Path $LogDir 'agent.err.log'

Write-Host "[ME_Agent] 检查通过, 后台静默启动..." -ForegroundColor Green
$proc = Start-Process -FilePath $VenvPy -ArgumentList $pyArgs `
    -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog
$proc.Id | Out-File -FilePath (Join-Path $LogDir 'agent.pid') -Encoding ascii

$shownPort = if ($Port -gt 0) { $Port } else { 8000 }
Write-Host ""
Write-Host "[ME_Agent] 已启动(PID $($proc.Id))  http://127.0.0.1:$shownPort  (控制台: /console/index.html)" -ForegroundColor Green
Write-Host "  日志: $OutLog"
Write-Host "  停止: taskkill /PID $($proc.Id) /F"
Write-Host ""
exit 0
