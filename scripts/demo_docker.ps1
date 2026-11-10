<#
.SYNOPSIS
    Docker 隔离演示：stub 模型 + SQLite + 演示数据，一条命令起服务。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\demo_docker.ps1 -Build
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\demo_docker.ps1 -Reset -Port 8011
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts\demo_docker.ps1 -NoWait
#>
[CmdletBinding()]
param(
    [int]$Port = 8010,
    [switch]$Reset,
    [switch]$Build,
    [switch]$NoWait
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Compose = Join-Path $Root 'deploy\docker-compose.demo.yml'
$env:DEMO_PORT = "$Port"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw '找不到 docker CLI；本地非容器演示请用 scripts\demo.ps1'
}

if ($Reset) {
    Write-Host '停止并清空旧演示容器/数据...' -ForegroundColor Yellow
    & docker compose -f $Compose down --remove-orphans
    if ($LASTEXITCODE -ne 0) { throw "docker compose down 失败（$LASTEXITCODE）" }
}

$upArgs = @('compose', '-f', $Compose, 'up', '-d')
if ($Build) { $upArgs += '--build' }
& docker @upArgs
if ($LASTEXITCODE -ne 0) { throw "docker compose up 失败（$LASTEXITCODE）" }

Write-Host ''
Write-Host "演示地址：http://127.0.0.1:$Port/" -ForegroundColor Green
Write-Host '演示账号（虚构数据）：ops/ops123、reviewer/reviewer123、admin/admin123'
Write-Host '模型=stub；数据库=SQLite；不读取真实 Key；down 即清空。' -ForegroundColor Yellow
Write-Host ''

if ($NoWait) { exit 0 }

$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 3
        if ($health.status -eq 'ok') { $ready = $true; break }
    } catch { }
}
if (-not $ready) {
    & docker compose -f $Compose logs --tail 80 demo
    throw "演示容器未在 60 秒内就绪（端口 $Port）"
}

Write-Host '就绪：/health=ok' -ForegroundColor Green
Write-Host '停止：docker compose -f deploy/docker-compose.demo.yml down' -ForegroundColor Cyan
