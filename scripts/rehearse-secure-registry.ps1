param(
    [string]$PythonPath = ".venv\Scripts\python.exe",
    [string]$RegistryProject = "pic-secure-registry-verify",
    [int]$RegistryPort = 15443,
    [string]$MaterialJsonOut = ".tmp\release\secure-registry-material-p3-27.json",
    [string]$JsonOut = ".tmp\release\secure-registry-p3-27.json"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = if ([System.IO.Path]::IsPathRooted($PythonPath)) { $PythonPath } else { Join-Path $Root $PythonPath }
$Compose = Join-Path $Root "deploy\docker-compose.secure-registry.yml"
$Prepare = Join-Path $Root "backend\scripts\prepare_secure_registry.py"
$Verifier = Join-Path $Root "backend\scripts\verify_secure_registry.py"
$MaterialOutput = if ([System.IO.Path]::IsPathRooted($MaterialJsonOut)) { $MaterialJsonOut } else { Join-Path $Root $MaterialJsonOut }
$Output = if ([System.IO.Path]::IsPathRooted($JsonOut)) { $JsonOut } else { Join-Path $Root $JsonOut }
$ConfigDir = Join-Path $Root (".tmp\secure-registry-" + [guid]::NewGuid().ToString("N"))
$ExpectedPrefix = (Join-Path $Root ".tmp\secure-registry-")
$RegistryStarted = $false
$PreviousConfigDir = $env:SECURE_REGISTRY_CONFIG_DIR
$PreviousPort = $env:SECURE_REGISTRY_PORT
$PreviousUsername = $env:SECURE_REGISTRY_USERNAME
$PreviousPassword = $env:SECURE_REGISTRY_PASSWORD

function Assert-ExitCode([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw "$Message (exit=$LASTEXITCODE)" }
}

function Assert-ProjectAbsent([string]$Project) {
    if ($Project -notmatch '^pic-[a-z0-9][a-z0-9-]{2,62}$') { throw "Invalid Registry project name" }
    $raw = docker compose ls --format json
    Assert-ExitCode "Cannot list Compose projects"
    if (-not $raw.Trim()) { return }
    $projects = @($raw | ConvertFrom-Json)
    if ($projects.Name -contains $Project) { throw "Refusing to replace existing Compose project: $Project" }
}

foreach ($required in ($Python, $Compose, $Prepare, $Verifier)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required file not found: $required" }
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "docker was not found" }

Push-Location $Root
try {
    $dirty = git status --porcelain
    Assert-ExitCode "Cannot inspect Git worktree"
    if ($dirty) { throw "Refusing secure Registry rehearsal from a dirty Git worktree" }
    $ReleaseSha = (git rev-parse HEAD).Trim()
    if ($ReleaseSha -notmatch '^[0-9a-f]{40}$') { throw "Git HEAD is not a full SHA" }
    Assert-ProjectAbsent $RegistryProject

    $env:SECURE_REGISTRY_USERNAME = "pic-ci"
    $PasswordBytes = New-Object byte[] 32
    $Random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $Random.GetBytes($PasswordBytes)
    }
    finally {
        $Random.Dispose()
    }
    $env:SECURE_REGISTRY_PASSWORD = [Convert]::ToBase64String($PasswordBytes)
    $env:SECURE_REGISTRY_CONFIG_DIR = $ConfigDir
    $env:SECURE_REGISTRY_PORT = [string]$RegistryPort

    & $Python $Prepare --output-dir $ConfigDir --json-out $MaterialOutput
    Assert-ExitCode "Secure Registry material generation failed"

    $RegistryStarted = $true
    docker compose -p $RegistryProject -f $Compose up -d
    Assert-ExitCode "Secure Registry startup failed"

    & $Python $Verifier `
        --endpoint "https://127.0.0.1:$RegistryPort" `
        --ca-cert (Join-Path $ConfigDir "certs\ca.crt") `
        --source-sha $ReleaseSha `
        --json-out $Output
    Assert-ExitCode "Secure Registry verification failed"
    Write-Host "Secure Registry rehearsal passed: $Output"
}
finally {
    if ($RegistryStarted) {
        docker compose -p $RegistryProject -f $Compose down -v --remove-orphans | Out-Null
    }
    if (Test-Path -LiteralPath $ConfigDir) {
        $ResolvedConfig = (Resolve-Path -LiteralPath $ConfigDir).Path
        if (-not $ResolvedConfig.StartsWith($ExpectedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove unexpected secure Registry directory"
        }
        Remove-Item -LiteralPath $ResolvedConfig -Recurse -Force
    }
    $env:SECURE_REGISTRY_CONFIG_DIR = $PreviousConfigDir
    $env:SECURE_REGISTRY_PORT = $PreviousPort
    $env:SECURE_REGISTRY_USERNAME = $PreviousUsername
    $env:SECURE_REGISTRY_PASSWORD = $PreviousPassword
    Pop-Location
}
