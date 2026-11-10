param(
    [string]$PythonPath = ".venv\Scripts\python.exe",
    [string]$RegistryProject = "pic-attestation-verify",
    [int]$RegistryPort = 15000,
    [string]$Repository = "pic-api",
    [string]$JsonOut = ".tmp\release\image-attestations-p3-21.json",
    [switch]$KeepResources
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = if ([System.IO.Path]::IsPathRooted($PythonPath)) {
    $PythonPath
} else {
    Join-Path $Root $PythonPath
}
$RegistryCompose = Join-Path $Root "deploy\docker-compose.local-registry.yml"
$Dockerfile = Join-Path $Root "backend\Dockerfile"
$Verifier = Join-Path $Root "backend\scripts\verify_image_attestations.py"
$Output = if ([System.IO.Path]::IsPathRooted($JsonOut)) { $JsonOut } else { Join-Path $Root $JsonOut }
$RegistryAuthority = "127.0.0.1:$RegistryPort"
$PreviousRegistryPort = [Environment]::GetEnvironmentVariable("LOCAL_REGISTRY_PORT", "Process")
$RegistryStarted = $false

function Assert-ExitCode([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw "$Message (exit=$LASTEXITCODE)" }
}

function Assert-ProjectAbsent([string]$Project) {
    $raw = docker compose ls --format json
    Assert-ExitCode "Cannot list Compose projects"
    if (-not $raw.Trim()) { return }
    $projects = @($raw | ConvertFrom-Json)
    if ($projects.Name -contains $Project) {
        throw "Refusing to replace existing Compose project: $Project"
    }
}

function Wait-Registry {
    foreach ($attempt in 1..30) {
        try {
            $response = Invoke-WebRequest -UseBasicParsing -Uri "http://$RegistryAuthority/v2/" -TimeoutSec 2
            if ($response.StatusCode -eq 200) { return }
        } catch {}
        Start-Sleep -Seconds 1
    }
    throw "Local Registry did not become ready within 30 seconds"
}

if (-not (Test-Path -LiteralPath $Python)) { throw "Python executable not found: $Python" }
foreach ($required in ($RegistryCompose, $Dockerfile, $Verifier)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required file not found: $required" }
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "docker was not found" }

Push-Location $Root
try {
    $dirty = git status --porcelain
    Assert-ExitCode "Cannot inspect Git worktree"
    if ($dirty) { throw "Refusing attestation build from a dirty Git worktree" }
    $ReleaseSha = (git rev-parse HEAD).Trim()
    Assert-ExitCode "Cannot resolve Git HEAD"
    if ($ReleaseSha -notmatch '^[0-9a-f]{40}$') { throw "Git HEAD is not a full SHA" }

    Assert-ProjectAbsent $RegistryProject
    $Tag = "p3-21-$($ReleaseSha.Substring(0, 12))"
    $ImageReference = "$RegistryAuthority/$Repository`:$Tag"

    Write-Host "[1/3] Start loopback-only Registry"
    $env:LOCAL_REGISTRY_PORT = [string]$RegistryPort
    docker compose -p $RegistryProject -f $RegistryCompose up -d
    Assert-ExitCode "Local Registry startup failed"
    $RegistryStarted = $true
    Wait-Registry

    Write-Host "[2/3] Build and push image with SLSA provenance and SPDX SBOM"
    docker buildx build `
        --file $Dockerfile `
        --tag $ImageReference `
        --build-arg "APP_RELEASE_SHA=$ReleaseSha" `
        --provenance=mode=max `
        --sbom=true `
        --push `
        $Root
    Assert-ExitCode "Attested image build failed"

    Write-Host "[3/3] Verify remote OCI index, provenance and SBOM"
    & $Python $Verifier `
        --image-reference $ImageReference `
        --expected-release-sha $ReleaseSha `
        --minimum-sbom-packages 50 `
        --evidence-scope local_loopback_registry `
        --json-out $Output
    Assert-ExitCode "Image attestation verification failed"
    Write-Host "Image attestation rehearsal passed: $Output"
}
finally {
    if (-not $KeepResources -and $RegistryStarted) {
        docker compose -p $RegistryProject -f $RegistryCompose down -v --remove-orphans | Out-Null
    }
    [Environment]::SetEnvironmentVariable("LOCAL_REGISTRY_PORT", $PreviousRegistryPort, "Process")
    Pop-Location
}
