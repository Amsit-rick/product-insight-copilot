<#
.SYNOPSIS
    电商售后工单智能处理系统 / After-sales Ticket Copilot —— 本地开发助手（Windows / PowerShell）。

.DESCRIPTION
    统一入口，避免手动切换目录、设置 PYTHONPATH、记忆长命令。

    子命令：
        setup   创建 .venv 并按哈希锁安装 backend\requirements-dev-windows.lock
        seed    导入种子 / 演示数据（存在种子脚本时）
        run     在 backend 目录启动 uvicorn（app.main:app，默认 127.0.0.1:8000）
        test    运行 pytest（backend\tests）
        eval    运行离线评测集（backend\tests\evaluation）
        lint    静态检查（ruff，缺失时退化为 python -m compileall）
        help    显示帮助

    示例：
        scripts\dev.ps1 setup
        scripts\dev.ps1 run            # http://127.0.0.1:8000/docs
        scripts\dev.ps1 run 8080       # 换端口
        scripts\dev.ps1 test
        scripts\dev.ps1 test tests\unit -v
        scripts\dev.ps1 eval
        scripts\dev.ps1 lint

.NOTES
    不依赖任何外部服务：默认 SQLite + 进程内缓存 + 本地文件存储即可跑通主流程。
    全栈容器化见 deploy/docker-compose.yml。

    若系统禁止运行脚本（SecurityError：在此系统上禁止运行脚本），可改用：
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev.ps1 run
    或为当前用户放开一次：
        Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command = 'run',

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest = @()
)

$ErrorActionPreference = 'Stop'

# -----------------------------------------------------------------------------
# 路径与全局状态
# -----------------------------------------------------------------------------
$Root         = Split-Path -Parent $PSScriptRoot          # 项目根目录（本脚本位于 scripts\ 下）
$BackendDir   = Join-Path $Root 'backend'
$VenvDir      = Join-Path $Root '.venv'
$VenvPython   = Join-Path $VenvDir 'Scripts\python.exe'
$Requirements = Join-Path $BackendDir 'requirements-dev-windows.lock'

$script:Python = @()

# -----------------------------------------------------------------------------
# 输出 helper
# -----------------------------------------------------------------------------
function Write-Step { param([string]$Message) Write-Host "[dev] $Message" -ForegroundColor Cyan }
function Write-Note { param([string]$Message) Write-Host "[dev] $Message" -ForegroundColor Yellow }
function Write-Ok   { param([string]$Message) Write-Host "[dev] $Message" -ForegroundColor Green }

function Fail {
    param(
        [Parameter(Position = 0)][string]$Message,
        [Parameter(Position = 1)][int]$Code = 1
    )
    Write-Host ''
    Write-Host '[错误] ' -ForegroundColor Red -NoNewline
    Write-Host $Message -ForegroundColor Red
    Write-Host ''
    exit $Code
}

function Show-Usage {
    Write-Host @'
电商售后工单智能处理系统 —— 本地开发助手

用法：scripts\dev.ps1 <子命令> [参数...]

  setup              创建 .venv 并按哈希锁安装 backend\requirements-dev-windows.lock
  seed               导入种子 / 演示数据（需要 backend 下存在种子脚本）
  run [端口]         启动 API 服务（默认 http://127.0.0.1:8000，带热重载）
  test [pytest参数]  运行 pytest（读取 backend\pytest.ini）
  eval [pytest参数]  运行离线评测集（backend\tests\evaluation）
  lint               静态检查（ruff；未安装时退化为语法检查）
  help               显示本帮助

常用示例：
  scripts\dev.ps1 setup
  scripts\dev.ps1 run
  scripts\dev.ps1 run 8080
  scripts\dev.ps1 test -v
  scripts\dev.ps1 eval
  scripts\dev.ps1 lint

默认配置即为本地零依赖模式：SQLite + 进程内缓存 + 本地文件存储 + stub 模型。
需要 Postgres / Redis / MinIO / 真实模型时，复制 backend\.env.example 为 backend\.env，
或使用容器编排：docker compose -f deploy/docker-compose.yml up -d --build

若提示"在此系统上禁止运行脚本"，改用：
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts\dev.ps1 run
或为当前用户放开一次：Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
'@
}

# -----------------------------------------------------------------------------
# Python 解释器解析
# 优先级：<项目根>\.venv\Scripts\python.exe → py -3.12 → python
# -----------------------------------------------------------------------------
function Resolve-Python {
    if (Test-Path -LiteralPath $VenvPython) {
        return @($VenvPython)
    }

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        Write-Note "未找到 $VenvPython，回退使用 py -3.12（建议先执行 scripts\dev.ps1 setup）。"
        return @($pyLauncher.Source, '-3.12')
    }

    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCmd) {
        Write-Note "未找到 $VenvPython，回退使用系统 python：$($pythonCmd.Source)"
        return @($pythonCmd.Source)
    }

    Fail '未找到可用的 Python 解释器。请先安装 Python 3.12，再执行 scripts\dev.ps1 setup。'
    return @()
}

function Initialize-Python {
    if ($script:Python.Count -eq 0) {
        $script:Python = @(Resolve-Python)
        Write-Step "Python 解释器：$($script:Python -join ' ')"
    }
}

# 统一调用：解释器前缀 + 调用方参数，逐元素透传给原生进程
function Invoke-Python {
    param([string[]]$Arguments = @())

    Initialize-Python

    $all = @()
    $all += $script:Python
    if ($Arguments) { $all += $Arguments }

    $exe = [string]$all[0]
    if ($all.Count -gt 1) {
        $rest = @($all[1..($all.Count - 1)])
        & $exe @rest
    }
    else {
        & $exe
    }
}

# -----------------------------------------------------------------------------
# 依赖探测：必须能 import fastapi，否则明确报错（本机可能无外网，无法自动安装）
# -----------------------------------------------------------------------------
function Test-Dependencies {
    Initialize-Python

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $code = 1
    try {
        Invoke-Python -Arguments @('-c', 'import fastapi') 2>$null | Out-Null
        $code = $LASTEXITCODE
    }
    catch {
        $code = 1
    }
    finally {
        $ErrorActionPreference = $previous
    }
    return ($code -eq 0)
}

function Assert-Dependencies {
    if (Test-Dependencies) { return }
    Fail @"
当前 Python 解释器缺少项目依赖（探测 import fastapi 失败）。
       解释器：$($script:Python -join ' ')
       解决：  scripts\dev.ps1 setup
               （创建 .venv 并执行 pip install --require-hashes -r backend\requirements-dev-windows.lock）
       提示：  若本机无法访问 PyPI，请改用已装好依赖的 .venv，或使用离线 wheel：
               python -m pip install --require-hashes --no-index --find-links <wheel目录> -r backend\requirements-dev-windows.lock
"@
}

# -----------------------------------------------------------------------------
# setup
# -----------------------------------------------------------------------------
function Invoke-Setup {
    if (-not (Test-Path -LiteralPath $Requirements)) {
        Fail "找不到依赖清单：$Requirements"
    }

    if (-not (Test-Path -LiteralPath $VenvPython)) {
        Write-Step "创建虚拟环境：$VenvDir"

        $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
        if ($pyLauncher) {
            & $pyLauncher.Source -3.12 -m venv $VenvDir
        }
        else {
            $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
            if (-not $pythonCmd) { Fail '未找到 Python 解释器，请先安装 Python 3.12。' }
            & $pythonCmd.Source -m venv $VenvDir
        }

        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
            Fail "创建虚拟环境失败（退出码 $LASTEXITCODE）。"
        }
        Write-Ok "虚拟环境已创建：$VenvPython"
    }
    else {
        Write-Step "复用已存在的虚拟环境：$VenvDir"
    }

    Write-Step '安装依赖：python -m pip install --require-hashes -r backend\requirements-dev-windows.lock'
    & $VenvPython -m pip install --require-hashes -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        Fail "依赖安装失败（退出码 $LASTEXITCODE）。若本机无外网，请使用离线 wheel 目录安装，或直接复用已配置好的 .venv。"
    }

    Write-Ok '环境就绪。接下来可以执行：scripts\dev.ps1 run'
}

# -----------------------------------------------------------------------------
# seed
# -----------------------------------------------------------------------------
function Find-SeedScript {
    $candidates = @(
        'app\seed.py',
        'app\db\seed.py',
        'scripts\seed.py',
        'scripts\seed_demo.py'
    )
    foreach ($relative in $candidates) {
        $full = Join-Path $BackendDir $relative
        if (Test-Path -LiteralPath $full) { return $full }
    }

    $found = Get-ChildItem -LiteralPath $BackendDir -Recurse -File -Filter 'seed*.py' -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '__pycache__' -and $_.FullName -notmatch '\\tests\\' } |
        Sort-Object -Property FullName |
        Select-Object -First 1
    if ($found) { return $found.FullName }
    return $null
}

function Invoke-Seed {
    Assert-Dependencies

    $seedScript = Find-SeedScript
    if (-not $seedScript) {
        Fail @"
未找到种子数据脚本，无法导入演示数据。
       期望位置（任选其一）：
         backend\app\seed.py
         backend\app\db\seed.py
         backend\scripts\seed.py
         backend\scripts\seed_demo.py
       说明：仓库尚未提供种子脚本时属正常情况。也可以手工准备数据：
             上传文档走 POST /api/v1/documents，指标导入走 POST /api/v1/metrics/import。
"@
    }

    Write-Step "导入种子数据：$seedScript"
    Push-Location $BackendDir
    try {
        Invoke-Python -Arguments @($seedScript)
    }
    finally {
        Pop-Location
    }

    if ($LASTEXITCODE -ne 0) {
        Fail "种子数据导入失败（退出码 $LASTEXITCODE）。"
    }
    Write-Ok '种子数据导入完成。'
}

# -----------------------------------------------------------------------------
# run
# -----------------------------------------------------------------------------
function Invoke-Run {
    Assert-Dependencies

    $port = 8000
    $extra = @()
    if ($Rest.Count -ge 1 -and $Rest[0]) {
        $parsed = 0
        if (-not [int]::TryParse($Rest[0], [ref]$parsed)) {
            Fail "端口号无效：$($Rest[0])（示例：scripts\dev.ps1 run 8080）。"
        }
        $port = $parsed
    }
    if ($Rest.Count -gt 1) { $extra = @($Rest[1..($Rest.Count - 1)]) }

    $mainPy = Join-Path $BackendDir 'app\main.py'
    if (-not (Test-Path -LiteralPath $mainPy)) {
        Write-Note "警告：未找到 $mainPy，服务入口约定为 app.main:app，uvicorn 可能启动失败。"
    }

    Write-Host ''
    Write-Ok   "  服务地址: http://127.0.0.1:$port"
    Write-Host "  接口文档: http://127.0.0.1:$port/docs" -ForegroundColor Green
    Write-Host "  健康检查: http://127.0.0.1:$port/health" -ForegroundColor Green
    Write-Host '  结束服务: Ctrl + C' -ForegroundColor DarkGray
    Write-Host ''

    Push-Location $BackendDir
    try {
        $arguments = @('-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$port", '--reload', '--no-proxy-headers')
        if ($extra) { $arguments += $extra }
        Invoke-Python -Arguments $arguments
    }
    finally {
        Pop-Location
    }

    if ($LASTEXITCODE -ne 0) {
        Fail "uvicorn 退出（退出码 $LASTEXITCODE）。请检查 backend\app\main.py 是否存在、依赖是否安装完整。"
    }
}

# -----------------------------------------------------------------------------
# test / eval
# -----------------------------------------------------------------------------
function Invoke-Tests {
    Assert-Dependencies

    $testsDir = Join-Path $BackendDir 'tests'
    if (-not (Test-Path -LiteralPath $testsDir)) {
        Fail @"
未找到测试目录：$testsDir
       请先创建 backend\tests（unit / integration / evaluation 子目录），
       再执行 scripts\dev.ps1 test。
"@
    }

    Write-Step '运行 pytest（backend\pytest.ini：testpaths=tests, pythonpath=.）'
    Push-Location $BackendDir
    try {
        $arguments = @('-m', 'pytest')
        if ($Rest) { $arguments += $Rest }
        Invoke-Python -Arguments $arguments
    }
    finally {
        Pop-Location
    }

    if ($LASTEXITCODE -ne 0) {
        Fail "pytest 未通过（退出码 $LASTEXITCODE）。" $LASTEXITCODE
    }
    Write-Ok '测试全部通过。'
}

function Invoke-Eval {
    Assert-Dependencies

    $evalDir   = Join-Path $BackendDir 'tests\evaluation'
    $evalEntry = Join-Path $BackendDir 'app\evaluation\run.py'
    $arguments = @()

    if (Test-Path -LiteralPath $evalDir) {
        Write-Step '运行离线评测集：backend\tests\evaluation'
        $arguments = @('-m', 'pytest', 'tests/evaluation')
    }
    elseif (Test-Path -LiteralPath $evalEntry) {
        Write-Step "运行离线评测入口：$evalEntry"
        $arguments = @('-m', 'app.evaluation.run')
    }
    else {
        Fail @"
未找到离线评测集，无法执行评测。
       期望位置（任选其一，对应设计文档 16.2）：
         backend\tests\evaluation\        （pytest 形式，推荐）
         backend\app\evaluation\run.py
       评测需要覆盖：单知识库 / 跨知识库 / 无答案 / 冲突证据 / 版本时间错配 /
                     数据缺失 / 越权请求 / Prompt 注入。
"@
    }

    if ($Rest) { $arguments += $Rest }

    Push-Location $BackendDir
    try {
        Invoke-Python -Arguments $arguments
    }
    finally {
        Pop-Location
    }

    if ($LASTEXITCODE -ne 0) {
        Fail "评测未通过（退出码 $LASTEXITCODE）。" $LASTEXITCODE
    }
    Write-Ok '评测通过。'
}

# -----------------------------------------------------------------------------
# lint
# -----------------------------------------------------------------------------
function Invoke-Lint {
    Assert-Dependencies

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $hasRuff = $false
    try {
        Invoke-Python -Arguments @('-m', 'ruff', '--version') 2>$null | Out-Null
        $hasRuff = ($LASTEXITCODE -eq 0)
    }
    catch {
        $hasRuff = $false
    }
    finally {
        $ErrorActionPreference = $previous
    }

    Push-Location $BackendDir
    try {
        if ($hasRuff) {
            Write-Step '使用 ruff 进行静态检查'
            $arguments = @('-m', 'ruff', 'check', '.')
        }
        else {
            Write-Note '未安装 ruff / flake8，退化为语法检查（python -m compileall）。安装后可获得完整静态检查：pip install ruff'
            $arguments = @('-m', 'compileall', '-q', '.')
        }
        if ($Rest) { $arguments += $Rest }
        Invoke-Python -Arguments $arguments
    }
    finally {
        Pop-Location
    }

    if ($LASTEXITCODE -ne 0) {
        Fail "静态检查未通过（退出码 $LASTEXITCODE）。" $LASTEXITCODE
    }
    Write-Ok '静态检查通过。'
}

# -----------------------------------------------------------------------------
# 主流程
# -----------------------------------------------------------------------------
if (-not (Test-Path -LiteralPath $BackendDir)) {
    Fail "找不到后端目录：$BackendDir（请确认本脚本位于项目根目录的 scripts\ 下）。"
}

$sub = "$Command".Trim().ToLowerInvariant()
if ($sub -eq '-h' -or $sub -eq '--help') { $sub = 'help' }

if ($sub -eq 'help') {
    Show-Usage
    exit 0
}

# PYTHONPATH 指向 backend，保证 `import app.xxx` 在任意子命令下都可用
if ($env:PYTHONPATH) {
    $env:PYTHONPATH = "$BackendDir;$env:PYTHONPATH"
}
else {
    $env:PYTHONPATH = $BackendDir
}

Write-Host ''
Write-Step "项目根目录：$Root"
Write-Step "后端目录  ：$BackendDir"

switch ($sub) {
    'setup' { Invoke-Setup }
    'seed'  { Invoke-Seed }
    'run'   { Invoke-Run }
    'test'  { Invoke-Tests }
    'eval'  { Invoke-Eval }
    'lint'  { Invoke-Lint }
    default {
        Show-Usage
        Fail "未知子命令：$Command。可用子命令：setup | seed | run | test | eval | lint | help" 2
    }
}
