<#
.SYNOPSIS
    电商售后工单智能处理系统 —— 一键 CI（本地与流水线共用同一套检查）。

.NOTES
    ⚠️ 本文件必须保存为 **UTF-8 with BOM**：Windows PowerShell 5.1 在无 BOM 时会按系统
    ANSI 代码页解析，中文会被拆成乱码字节并导致"字符串未闭合"之类的语法错误
    （踩过一次，报错信息还看不出真正原因）。用 VS Code 保存时选 "UTF-8 with BOM"。
#>
<#
.DESCRIPTION
    按顺序执行，任一步失败即非零退出：

      1. 后端全量测试        backend\tests（离线可跑，绝不调用付费模型）
      1.1 Compose 静态校验   docker compose config --quiet（不启动容器、不需要 Docker daemon）
      1.4 Webhook 本地闭环    回环接收器验证签名、重试、死信与重放（不访问外网）
      1.5 发布前自检         preflight_check.py（常规档）
      1.51 政策检索门禁      Recall@5 / MRR 基准（独立临时库 + stub，零成本）
      1.6 售后工单离线评测   run_ticket_evaluation.py（独立临时库 + stub，零成本）
      2. 前端类型检查        vue-tsc --noEmit（覆盖 .ts 与 .vue 模板）
      3. 前端生产构建        vite build（产物由后端同源托管）
      4. 前端端到端冒烟      -WithE2E 时执行：脚本会自己拉起**stub 模式后端**与无头 Chrome，
                             跑完自动清理（不需要你手动开服务，也不会花钱调模型）；
                             同一台 stub 后端上还会跑两个 live 校验脚本：
                             verify_tickets.py（58 项）与 verify_ticket_l2_send.py（15 项），
                             以及售后角色化回归 after-sales.mjs。

    为什么 e2e 要自己起服务：冒烟测试最怕"跑的是昨天的进程"。
    这里用独立的端口 + 独立的临时库 + LLM_PROVIDER=stub，保证结果可重复、不污染开发库。

    示例：
        scripts\ci.ps1                 # 后端测试 + 类型检查 + 构建
        scripts\ci.ps1 -WithE2E        # 再跑一遍浏览器端冒烟（需要本机装了 Chrome）
        scripts\ci.ps1 -SkipFrontend   # 只跑后端（改后端时的快速回归）
        scripts\ci.ps1 -SkipCompose   # 仅在没有 Docker CLI 时显式跳过 Compose 静态校验
        scripts\ci.ps1 -StrictPreflight # 在当前环境执行生产配置门禁（配置不满足时应失败）
        scripts\ci.ps1 -PythonPath backend\.tmp\clean-venv\Scripts\python.exe
                                      # 指定隔离解释器，审计环境污染时使用

.NOTES
    若系统禁止运行脚本：
        powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ci.ps1 -WithE2E
#>
[CmdletBinding()]
param(
    [switch]$WithE2E,
    [switch]$SkipFrontend,
    [switch]$SkipCompose,
    [switch]$StrictPreflight,
    [string]$PythonPath = '',
    [int]$E2EPort = 8199,
    [int]$CdpPort = 9224
)

$ErrorActionPreference = 'Stop'

$Root       = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $Root 'backend'
$FrontendDir = Join-Path $Root 'frontend'
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
$NodeExe    = 'node'
$PolicyBenchmarkEvidence = Join-Path $BackendDir '.tmp\policy-retrieval-benchmark-ci.json'

$script:Failures = @()
$script:Started = @()

function Write-Section { param([string]$Text) Write-Host "`n=== $Text ===" -ForegroundColor Cyan }
function Write-Ok      { param([string]$Text) Write-Host "  [PASS] $Text" -ForegroundColor Green }
function Write-Bad     { param([string]$Text) Write-Host "  [FAIL] $Text" -ForegroundColor Red }
function Write-Note    { param([string]$Text) Write-Host "  [note] $Text" -ForegroundColor Yellow }

function Invoke-Step {
    param([string]$Name, [scriptblock]$Body)
    Write-Section $Name
    try {
        & $Body
        Write-Ok $Name
    }
    catch {
        Write-Bad "$Name —— $($_.Exception.Message)"
        $script:Failures += $Name
    }
}

function Get-Python {
    if ($PythonPath) {
        $resolved = Resolve-Path -LiteralPath $PythonPath -ErrorAction SilentlyContinue
        if (-not $resolved) { throw "指定的 Python 不存在：$PythonPath" }
        return $resolved.Path
    }
    if (Test-Path -LiteralPath $VenvPython) { return $VenvPython }
    $fallback = (Get-Command python -ErrorAction SilentlyContinue)
    if ($fallback) {
        Write-Note "未找到 .venv，回退到 $($fallback.Source)（建议先跑 scripts\dev.ps1 setup）"
        return $fallback.Source
    }
    throw '找不到 Python 解释器：请先执行 scripts\dev.ps1 setup'
}

function Stop-Started {
    foreach ($proc in $script:Started) {
        if ($proc -and -not $proc.HasExited) {
            Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

try {
    # ---------------------------------------------------------- 0) Python 哈希锁
    Invoke-Step 'Python 依赖哈希锁校验' {
        $python = Get-Python
        Push-Location $Root
        try {
            & $python backend\scripts\verify_dependency_locks.py
            if ($LASTEXITCODE -ne 0) { throw "依赖锁校验退出码 $LASTEXITCODE" }
        }
        finally { Pop-Location }
    }

    # ---------------------------------------------------------------- 0) Compose 静态校验
    # 只解析 YAML 与变量展开，不连接 Docker daemon，不创建/修改容器或数据卷。
    if ($SkipCompose) {
        Write-Section 'Docker Compose 静态校验'
        Write-Note '已显式跳过（-SkipCompose）；本轮 CI 不包含编排文件门禁。'
    }
    else {
        Invoke-Step 'Docker Compose 静态校验' {
            $docker = Get-Command docker -ErrorAction SilentlyContinue
            if (-not $docker) { throw '找不到 Docker CLI；如仅需运行 Python/前端回归，请显式使用 -SkipCompose' }
            & $docker.Source compose -f (Join-Path $Root 'deploy\docker-compose.yml') config --quiet
            if ($LASTEXITCODE -ne 0) { throw "docker compose config 退出码 $LASTEXITCODE" }
            $previousImage = $env:PIC_API_IMAGE
            $previousPullPolicy = $env:PIC_RELEASE_PULL_POLICY
            try {
                $env:PIC_API_IMAGE = 'registry.invalid/pic-api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
                $env:PIC_RELEASE_PULL_POLICY = 'never'
                & $docker.Source compose `
                    -f (Join-Path $Root 'deploy\docker-compose.yml') `
                    -f (Join-Path $Root 'deploy\docker-compose.release.yml') `
                    --profile proactive --profile sandbox config --quiet
                if ($LASTEXITCODE -ne 0) { throw "release compose config 退出码 $LASTEXITCODE" }
            }
            finally {
                $env:PIC_API_IMAGE = $previousImage
                $env:PIC_RELEASE_PULL_POLICY = $previousPullPolicy
            }
        }
    }

    # ---------------------------------------------------------------- 1) 后端测试
    Invoke-Step '后端全量测试（backend\tests，离线）' {
        $python = Get-Python
        Push-Location $BackendDir
        try {
            $env:PYTHONPATH = $BackendDir
            & $python scripts\pytest_with_dump.py tests -q --dump-after 900
            if ($LASTEXITCODE -ne 0) { throw "pytest 退出码 $LASTEXITCODE" }
        }
        finally { Pop-Location }
    }

    # ------------------------------------------- 1.1) 发布报告关键路径覆盖率
    Invoke-Step '发布报告关键路径覆盖率（不低于 80%）' {
        $python = Get-Python
        $previousCoverageFile = $env:COVERAGE_FILE
        Push-Location $BackendDir
        try {
            $env:PYTHONPATH = $BackendDir
            $env:COVERAGE_FILE = Join-Path $BackendDir '.tmp\.coverage.release-report'
            & $python -m coverage erase
            if ($LASTEXITCODE -ne 0) { throw "coverage erase 退出码 $LASTEXITCODE" }
            & $python -m coverage run `
                --branch `
                --include='scripts/after_sales_release_report.py' `
                -m pytest tests/unit/test_after_sales_release_report.py -q
            if ($LASTEXITCODE -ne 0) { throw "coverage run 退出码 $LASTEXITCODE" }
            & $python -m coverage report `
                --include='scripts/after_sales_release_report.py' `
                --fail-under=80
            if ($LASTEXITCODE -ne 0) { throw "coverage report 退出码 $LASTEXITCODE" }
            $coverageJson = Join-Path $BackendDir '.tmp\coverage-release-report.json'
            & $python -m coverage json `
                --include='scripts/after_sales_release_report.py' `
                -o $coverageJson
            if ($LASTEXITCODE -ne 0) { throw "coverage json 退出码 $LASTEXITCODE" }
            & $python scripts\verify_coverage_report.py --coverage-json $coverageJson --file scripts/after_sales_release_report.py --min-statements 80 --min-branches 80
            if ($LASTEXITCODE -ne 0) { throw "覆盖率语句/分支门禁退出码 $LASTEXITCODE" }
        }
        finally {
            $env:COVERAGE_FILE = $previousCoverageFile
            Pop-Location
        }
    }

    # ------------------------------------------- 1.4) Webhook 本地闭环
    # 使用 127.0.0.1 回环接收器和生产 AlertDeliveryWorker；不会访问配置中的外部 URL。
    Invoke-Step 'Webhook 本地闭环验收（不访问外网）' {
        $python = Get-Python
        Push-Location $Root
        try {
            $env:PYTHONPATH = $BackendDir
            & $python backend\scripts\verify_webhook_integration.py `
                --json-out docs\testing\artifacts\webhook-integration-local-check.json
            if ($LASTEXITCODE -ne 0) { throw "Webhook 本地闭环验收退出码 $LASTEXITCODE" }
        }
        finally { Pop-Location }
    }

    # ------------------------------------------- 1.5) 发布前自检
    # 默认跑常规档，保持本地开发体验；显式 -StrictPreflight 才按生产标准执行。
    $preflightName = if ($StrictPreflight) { '发布前自检（preflight，严格生产档）' } else { '发布前自检（preflight，常规档）' }
    Invoke-Step $preflightName {
        $python = Get-Python
        Push-Location $BackendDir
        try {
            $env:PYTHONPATH = $BackendDir
            if ($StrictPreflight) {
                & $python scripts\preflight_check.py --strict
            }
            else {
                & $python scripts\preflight_check.py
            }
            if ($LASTEXITCODE -ne 0) { throw "preflight 发现必须修复项（退出码 $LASTEXITCODE）" }
        }
        finally { Pop-Location }
    }

    # ------------------------------------------- 1.51) 政策检索 Recall@5
    # 独立 SQLite + stub embedding，不调用真实模型；输出到 .tmp，避免刷新证据时把工作区变脏。
    Invoke-Step '政策检索 Recall@5 门禁（stub，零成本）' {
        $python = Get-Python
        Push-Location $BackendDir
        try {
            $env:PYTHONPATH = $BackendDir
            & $python scripts\benchmark_policy_retrieval.py `
                --json-out $PolicyBenchmarkEvidence
            if ($LASTEXITCODE -ne 0) { throw "政策检索 Recall@5 门禁退出码 $LASTEXITCODE" }
        }
        finally { Pop-Location }
    }

    # ------------------------------------------- 1.55) 售后预发布报告
    # 报告诚实记录外部门禁未完成；本地 CI 只负责刷新证据产物，
    # 不因为尚未接入真实平台/模型而把离线开发检查误报为失败。
    Invoke-Step '售后预发布报告（刷新本地证据）' {
        $python = Get-Python
        Push-Location $Root
        try {
            $env:PYTHONPATH = $BackendDir
            & $python backend\scripts\after_sales_release_report.py `
                --policy-benchmark-evidence $PolicyBenchmarkEvidence `
                --json-out docs\testing\artifacts\after-sales-release-readiness.json
            if ($LASTEXITCODE -ne 0) { throw "售后预发布报告退出码 $LASTEXITCODE" }
        }
        finally { Pop-Location }
    }

    # ------------------------------------------- 1.56) 本地交付候选检查
    Invoke-Step '售后本地交付候选检查' {
        $python = Get-Python
        Push-Location $Root
        try {
            $env:PYTHONPATH = $BackendDir
            & $python backend\scripts\after_sales_delivery_check.py `
                --json-out docs\testing\artifacts\after-sales-delivery-check.json `
                --fail-if-not-local-demo
            if ($LASTEXITCODE -ne 0) { throw "售后交付候选检查退出码 $LASTEXITCODE" }
        }
        finally { Pop-Location }
    }

    # ------------------------------------------- 1.565) 原子交付证据包
    Invoke-Step '售后交付证据 manifest（同批次 + SHA-256）' {
        $python = Get-Python
        Push-Location $Root
        try {
            $env:PYTHONPATH = $BackendDir
            & $python backend\scripts\package_delivery_evidence.py `
                --evidence-dir docs\testing\artifacts
            if ($LASTEXITCODE -ne 0) { throw "售后交付证据打包退出码 $LASTEXITCODE" }
        }
        finally { Pop-Location }
    }

    # ------------------------------------------- 1.57) 交付摘要
    Invoke-Step '售后候选版本交付摘要' {
        $python = Get-Python
        Push-Location $Root
        try {
            $env:PYTHONPATH = $BackendDir
            & $python backend\scripts\after_sales_delivery_summary.py `
                --markdown-out docs\product-redesign\售后候选版本交付摘要.md
            if ($LASTEXITCODE -ne 0) { throw "售后交付摘要退出码 $LASTEXITCODE" }
        }
        finally { Pop-Location }
    }

    # ------------------------------------------- 1.6) 售后工单离线评测（stub，独立临时库）
    Invoke-Step '售后工单离线评测（stub，零成本）' {
        $python = Get-Python
        Push-Location $BackendDir
        try {
            $env:PYTHONPATH = $BackendDir
            & $python scripts\run_ticket_evaluation.py
            if ($LASTEXITCODE -ne 0) { throw "售后工单评测退出码 $LASTEXITCODE" }
        }
        finally { Pop-Location }
    }

    if (-not $SkipFrontend) {
        # ------------------------------------------------------------ 2) 类型检查
        Invoke-Step '前端类型检查（vue-tsc --noEmit，含 .vue）' {
            Push-Location $FrontendDir
            try {
                & $NodeExe node_modules\vue-tsc\bin\vue-tsc.js --noEmit -p tsconfig.json
                if ($LASTEXITCODE -ne 0) { throw "vue-tsc 退出码 $LASTEXITCODE" }
            }
            finally { Pop-Location }
        }

        # ------------------------------------------------------------ 3) 生产构建
        Invoke-Step '前端生产构建（vite build）' {
            $assets = Join-Path $FrontendDir 'dist\assets'
            $buildOutput = ''
            Push-Location $FrontendDir
            try {
                # vite 会把依赖里的 PURE 注释警告写到 stderr 并以非零退出；
                # PowerShell 默认把子进程 stderr 当成错误记录（NativeCommandError）并中断，
                # 所以这里显式允许非终止错误，并**以 vite 自己的成功标记 + 产物存在**判定成功。
                #
                # 为什么不用"产物时间戳必须变新"：产物内容没变时 vite 不会重写文件，
                # 连续跑两次 CI 就会误报失败（我第一版就是这么写的，第二次运行直接红了）。
                $previous = $ErrorActionPreference
                $ErrorActionPreference = 'Continue'
                try {
                    $buildOutput = (& $NodeExe node_modules\vite\bin\vite.js build *>&1 | Out-String)
                    Write-Host $buildOutput
                }
                finally {
                    $ErrorActionPreference = $previous
                }
            }
            finally { Pop-Location }

            if ($buildOutput -notmatch 'built in') {
                throw 'vite 未报告构建成功（输出里没有 "built in"）'
            }
            $entry = Get-ChildItem -LiteralPath $assets -Filter 'index-*.js' -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if (-not $entry) { throw '构建产物缺失：frontend\dist\assets\index-*.js' }
            Write-Note "构建产物：$($entry.Name)（$([math]::Round($entry.Length / 1KB)) KB，$($entry.LastWriteTime.ToString('HH:mm:ss'))）"
        }
    }

    # ---------------------------------------------------------------- 4) e2e 冒烟
    if ($WithE2E) {
        Invoke-Step "前端端到端冒烟（stub 后端 :$E2EPort + 无头 Chrome :$CdpPort）" {
            $python = Get-Python
            $chrome = @(
                "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
                "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
                "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe"
            ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
            if (-not $chrome) { throw '未找到 Chrome / Edge，跳过 e2e 需要 -WithE2E 以外的检查' }

            # stub 后端：独立端口 + 独立临时库，不碰开发库、不调用模型
            $e2eDb = Join-Path $BackendDir ".tmp\ci_e2e_$E2EPort.db"
            Remove-Item -LiteralPath $e2eDb -Force -ErrorAction SilentlyContinue
            $env:PYTHONPATH = $BackendDir
            $env:LLM_PROVIDER = 'stub'
            $env:LLM_API_KEY = ''
            $env:AGENT_MODEL_OVERRIDES = '{}'
            $env:DATABASE_URL = "sqlite:///$($e2eDb -replace '\\', '/')"
            $env:AUTO_SEED = 'true'
            $server = Start-Process -FilePath $python `
                -ArgumentList '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', "$E2EPort", '--no-proxy-headers' `
                -WorkingDirectory $BackendDir -WindowStyle Hidden -PassThru
            $script:Started += $server

            $profile = Join-Path $BackendDir ".tmp\ci_chrome_$CdpPort"
            $browser = Start-Process -FilePath $chrome `
                -ArgumentList '--headless=new', "--remote-debugging-port=$CdpPort", "--user-data-dir=$profile", `
                              '--no-first-run', '--window-size=1440,1000', 'about:blank' -WindowStyle Hidden -PassThru
            $script:Started += $browser

            # 等后端就绪（最多 60 秒）
            $ready = $false
            foreach ($attempt in 1..30) {
                Start-Sleep -Seconds 2
                try {
                    $health = Invoke-RestMethod "http://127.0.0.1:$E2EPort/health/ready" -TimeoutSec 5
                    if ($health.status -eq 'ready') { $ready = $true; break }
                }
                catch { }
            }
            if (-not $ready) { throw "stub 后端未在 60 秒内就绪（端口 $E2EPort）" }

            Push-Location $Root
            try {
                $env:APP_URL = "http://127.0.0.1:$E2EPort/"
                $env:CDP_URL = "http://127.0.0.1:$CdpPort"
                $env:E2E_SHOT_DIR = (Join-Path $BackendDir '.tmp\e2e')

                & $python backend\scripts\verify_tickets.py --base-url "http://127.0.0.1:$E2EPort"
                if ($LASTEXITCODE -ne 0) { throw "售后 live 校验 verify_tickets.py 退出码 $LASTEXITCODE" }

                $env:API_BASE = "http://127.0.0.1:$E2EPort"
                & $python backend\scripts\verify_ticket_l2_send.py
                if ($LASTEXITCODE -ne 0) { throw "售后 live 校验 verify_ticket_l2_send.py 退出码 $LASTEXITCODE" }

                & $NodeExe frontend\e2e\smoke.mjs
                $code = $LASTEXITCODE
                if ($code -ne 0) { throw "通用 e2e 冒烟退出码 $code" }

                # 售后主线独立角色回归：同一个隔离 stub 后端和 CDP 浏览器，
                # 验证客服/主管入口、待我关注与模板维护入口的业务可见性。
                & $NodeExe frontend\e2e\after-sales.mjs
                $afterSalesCode = $LASTEXITCODE
                # 全平台 axe WCAG A/AA 扫描：独立新标签页，避免继承前一套 E2E 的页面状态。
                # 脚本默认还会覆盖登录、客服和主管四个核心状态；all 模式再覆盖历史诊断平台页面。
                $env:A11Y_SCOPE = 'all'
                & $NodeExe frontend\e2e\accessibility.mjs
                $accessibilityCode = $LASTEXITCODE
            }
            finally { Pop-Location }

            Stop-Started
            $script:Started = @()
            if ($afterSalesCode -ne 0) { throw "售后角色回归退出码 $afterSalesCode" }
            if ($accessibilityCode -ne 0) { throw "全平台 axe 可访问性扫描退出码 $accessibilityCode" }
        }
    }
    else {
        Write-Section '前端端到端冒烟'
        Write-Note '已跳过（加 -WithE2E 开启；需要本机有 Chrome/Edge）'
    }
}
finally {
    Stop-Started
}

Write-Section '汇总'
if ($script:Failures.Count -gt 0) {
    Write-Bad "失败 $($script:Failures.Count) 项：$($script:Failures -join '、')"
    exit 1
}
Write-Ok '全部检查通过'
exit 0
