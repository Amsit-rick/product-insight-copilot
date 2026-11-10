<#
.SYNOPSIS
    一键本地演示：隔离 SQLite + stub 模型 + 固定演示账号 + 同源前端。

.DESCRIPTION
    不碰开发库、不调用真实模型、不依赖 Redis/Postgres。
    首次运行会自动建库/播种；加 -Reset 清空演示库重来。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\demo.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\demo.ps1 -Reset -Port 8011
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\demo.ps1 -NoServe
#>
[CmdletBinding()]
param(
    [int]$Port = 8010,
    [switch]$Reset,
    [switch]$NoServe
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root 'backend'
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$DbPath = Join-Path $Backend '.tmp\demo.db'

if (-not (Test-Path -LiteralPath $Python)) {
    throw "找不到 Python：$Python。请先执行 scripts\dev.ps1 setup"
}
New-Item -ItemType Directory -Path (Join-Path $Backend '.tmp') -Force | Out-Null

# 前端未构建时先构建；已构建则直接复用，避免每次演示都等 8 秒。
$Index = Join-Path $Root 'frontend\dist\index.html'
if (-not (Test-Path -LiteralPath $Index)) {
    Write-Host '前端产物不存在，正在构建...' -ForegroundColor Yellow
    Push-Location (Join-Path $Root 'frontend')
    try {
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { throw "前端构建失败（退出码 $LASTEXITCODE）" }
    }
    finally { Pop-Location }
}

$env:PYTHONPATH = $Backend
$env:APP_ENV = 'local'
$env:LLM_PROVIDER = 'stub'
$env:EMBEDDING_PROVIDER = 'stub'
$env:LLM_API_KEY = ''
$env:AGENT_MODEL_OVERRIDES = '{}'
$env:CACHE_BACKEND = 'memory'
$env:STORAGE_BACKEND = 'local'
$env:AUTO_SEED = 'true'
$env:DATABASE_URL = 'sqlite:///' + ($DbPath -replace '\\', '/')

Push-Location $Backend
try {
    if ($Reset -or -not (Test-Path -LiteralPath $DbPath)) {
        Write-Host "初始化演示库：$DbPath" -ForegroundColor Cyan
        & $Python -m app.db.seed --reset
    } else {
        Write-Host "复用演示库：$DbPath（加 -Reset 可清空重来）" -ForegroundColor Cyan
        & $Python -m app.db.seed
    }
    if ($LASTEXITCODE -ne 0) { throw "播种失败（退出码 $LASTEXITCODE）" }
}
finally { Pop-Location }

Write-Host ''
Write-Host "演示地址：http://127.0.0.1:$Port/" -ForegroundColor Green
Write-Host '演示账号（虚构数据）：' -ForegroundColor Green
Write-Host '  ops / ops123          客服：我的待办、分析、改草稿、转派'
Write-Host '  reviewer / reviewer123 售后主管：审批、质检、政策库'
Write-Host '  admin / admin123      管理员：系统状态、队列、审计'
Write-Host ''
Write-Host '模型=stub（零成本）；数据库=隔离 SQLite；不会读写 backend/.env 里的真实 Key。' -ForegroundColor Yellow
Write-Host ''

if ($NoServe) {
    Write-Host '已指定 -NoServe，只准备数据，不启动服务。' -ForegroundColor Yellow
    exit 0
}

Push-Location $Backend
try {
    & $Python -m uvicorn app.main:app --host 127.0.0.1 --port $Port --no-proxy-headers
}
finally { Pop-Location }
