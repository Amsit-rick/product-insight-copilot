param(
    [string]$PythonPath = ".venv\Scripts\python.exe",
    [string]$CurrentImage = "pic-api:p3-19-final",
    [string]$PreviousImage = "pic-api:p3-19",
    [string]$RegistryProject = "pic-local-registry-verify",
    [string]$AppProject = "pic-registry-rollout-verify",
    [int]$RegistryPort = 15000,
    [int]$ApiPort = 18230,
    [int]$PostgresPort = 15462,
    [int]$RedisPort = 16409,
    [int]$MinioPort = 19030,
    [int]$MinioConsolePort = 19031,
    [string]$JsonOut = ".tmp\release\registry-rollback-p3-20.json",
    [string]$DatabaseRollbackEvidence = "",
    [switch]$RestoreCurrentBeforeExit,
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
$BaseCompose = Join-Path $Root "deploy\docker-compose.yml"
$ReleaseCompose = Join-Path $Root "deploy\docker-compose.release.yml"
$Verifier = Join-Path $Root "backend\scripts\verify_registry_artifact.py"
$DeploymentProbe = Join-Path $Root "backend\scripts\verify_compose_deployment.py"
$QueueProbe = Join-Path $Root "backend\scripts\benchmark_ticket_queue.py"
$DatabaseRollbackVerifier = Join-Path $Root "backend\scripts\verify_database_rollback_evidence.py"
$Output = if ([System.IO.Path]::IsPathRooted($JsonOut)) { $JsonOut } else { Join-Path $Root $JsonOut }
$RegistryAuthority = "127.0.0.1:$RegistryPort"
$BaseUrl = "http://127.0.0.1:$ApiPort"
$RegistryStarted = $false
$AppStarted = $false

$ManagedVariables = @(
    "LOCAL_REGISTRY_PORT",
    "PIC_API_IMAGE",
    "PIC_RELEASE_PULL_POLICY",
    "APP_RELEASE_SHA",
    "API_PORT",
    "POSTGRES_PORT",
    "REDIS_PORT",
    "MINIO_PORT",
    "MINIO_CONSOLE_PORT",
    "APP_ENV",
    "DEBUG",
    "AUTO_SEED",
    "STORAGE_BACKEND",
    "VECTOR_BACKEND",
    "TASK_EXECUTION_BACKEND",
    "WORKER_EMBEDDED",
    "CHECKPOINTER_BACKEND"
)
$PreviousEnvironment = @{}
foreach ($name in $ManagedVariables) {
    $PreviousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
}

function Assert-ExitCode([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw "$Message (exit=$LASTEXITCODE)" }
}

function Get-ImageRevision([string]$Image) {
    $raw = docker image inspect $Image | Out-String
    Assert-ExitCode "Cannot inspect image $Image"
    try {
        $payloads = @($raw | ConvertFrom-Json)
    } catch {
        throw "Cannot parse image metadata for $Image"
    }
    if ($payloads.Count -ne 1) {
        throw "Image inspection must return exactly one image: $Image"
    }
    $label = $payloads[0].Config.Labels.PSObject.Properties[
        "org.opencontainers.image.revision"
    ]
    $revision = if ($null -eq $label) { "" } else { [string]$label.Value }
    $revision = $revision.Trim()
    if ($revision -notmatch '^[0-9a-f]{40}$') {
        throw "Image $Image does not contain a valid OCI revision label"
    }
    return $revision
}

function Get-ImageMigrationInventory([string]$Image) {
    $raw = @(
        docker run --rm --entrypoint sh $Image -c `
            "set -eu; find /app/migrations -maxdepth 1 -type f -name '*.sql' -exec basename {} \; | sort"
    )
    Assert-ExitCode "Cannot inspect migration inventory for image $Image"
    $inventory = @(
        $raw |
            ForEach-Object { ([string]$_).Trim() } |
            Where-Object { $_ }
    )
    if ($inventory.Count -lt 1) {
        throw "Image $Image does not contain versioned migrations"
    }
    return $inventory
}

function Get-StringArraySha256([string[]]$Values) {
    $text = (($Values | Sort-Object) -join "`n") + "`n"
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha.Dispose()
    }
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

function Wait-Application {
    foreach ($attempt in 1..45) {
        try {
            $payload = Invoke-RestMethod -Uri "$BaseUrl/health/ready" -TimeoutSec 3
            if ($payload.status -eq "ready") { return }
        } catch {}
        Start-Sleep -Seconds 2
    }
    throw "Application did not become ready within 90 seconds"
}

function Wait-WorkersHealthy {
    foreach ($attempt in 1..45) {
        $containerIds = @(
            docker ps `
                --filter "label=com.docker.compose.project=$AppProject" `
                --filter "label=com.docker.compose.service=worker" `
                --format '{{.ID}}'
        )
        if ($LASTEXITCODE -eq 0 -and $containerIds.Count -ge 2) {
            $allHealthy = $true
            foreach ($containerId in $containerIds) {
                $health = docker inspect $containerId --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}not_configured{{end}}'
                if ($LASTEXITCODE -ne 0 -or $health.Trim() -ne "healthy") {
                    $allHealthy = $false
                    break
                }
            }
            if ($allHealthy) { return }
        }
        Start-Sleep -Seconds 2
    }
    throw "Two workers did not become healthy within 90 seconds"
}

function Wait-ApiHealthy {
    foreach ($attempt in 1..45) {
        $containerIds = @(
            docker ps `
                --filter "label=com.docker.compose.project=$AppProject" `
                --filter "label=com.docker.compose.service=api" `
                --format '{{.ID}}'
        )
        if ($LASTEXITCODE -eq 0 -and $containerIds.Count -eq 1) {
            $health = docker inspect $containerIds[0] --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}not_configured{{end}}'
            if ($LASTEXITCODE -eq 0 -and $health.Trim() -eq "healthy") { return }
        }
        Start-Sleep -Seconds 2
    }
    throw "API container did not become healthy within 90 seconds"
}

function Set-AppEnvironment([string]$ImageReference, [string]$ReleaseSha) {
    $env:PIC_API_IMAGE = $ImageReference
    $env:PIC_RELEASE_PULL_POLICY = "never"
    $env:APP_RELEASE_SHA = $ReleaseSha
    $env:API_PORT = [string]$ApiPort
    $env:POSTGRES_PORT = [string]$PostgresPort
    $env:REDIS_PORT = [string]$RedisPort
    $env:MINIO_PORT = [string]$MinioPort
    $env:MINIO_CONSOLE_PORT = [string]$MinioConsolePort
    $env:APP_ENV = "test"
    $env:DEBUG = "false"
    $env:AUTO_SEED = "true"
    $env:STORAGE_BACKEND = "minio"
    $env:VECTOR_BACKEND = "pgvector"
    $env:TASK_EXECUTION_BACKEND = "queue"
    $env:WORKER_EMBEDDED = "false"
    $env:CHECKPOINTER_BACKEND = "postgres"
}

function Publish-Image(
    [string]$Image,
    [string]$ReleaseSha,
    [string]$Tag,
    [string]$EvidencePath
) {
    & $Python $Verifier `
        --source-image $Image `
        --registry $RegistryAuthority `
        --repository pic-api `
        --tag $Tag `
        --expected-release-sha $ReleaseSha `
        --allow-insecure-localhost `
        --json-out $EvidencePath
    Assert-ExitCode "Registry verification failed for $Image"
    return Get-Content -Raw -LiteralPath $EvidencePath | ConvertFrom-Json
}

function Start-Application([string]$ImageReference, [string]$ReleaseSha) {
    Set-AppEnvironment $ImageReference $ReleaseSha
    docker compose -p $AppProject -f $BaseCompose -f $ReleaseCompose --profile minio `
        up -d --scale worker=2 --force-recreate
    Assert-ExitCode "Application Compose startup failed"
    $script:AppStarted = $true
    Wait-Application
    Wait-ApiHealthy
    Wait-WorkersHealthy
}

function Invoke-DeploymentProbe(
    [string]$ImageReference,
    [string]$ReleaseSha,
    [string]$EvidencePath
) {
    & $Python $DeploymentProbe `
        --project $AppProject `
        --base-url $BaseUrl `
        --expected-env test `
        --expected-workers 2 `
        --expected-release-sha $ReleaseSha `
        --expected-image $ImageReference `
        --allow-insecure-http `
        --json-out $EvidencePath
    Assert-ExitCode "Compose deployment verification failed"
}

function Invoke-QueueProbe([string]$EvidencePath) {
    & $Python $QueueProbe `
        --base-url $BaseUrl `
        --count 4 `
        --submit-concurrency 2 `
        --expected-workers 2 `
        --timeout 120 `
        --json-out $EvidencePath
    Assert-ExitCode "Queue verification failed"
}

if (-not (Test-Path -LiteralPath $Python)) { throw "Python executable not found: $Python" }
foreach ($required in ($RegistryCompose, $BaseCompose, $ReleaseCompose, $Verifier, $DeploymentProbe, $QueueProbe, $DatabaseRollbackVerifier)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Required file not found: $required" }
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "docker was not found" }

Assert-ProjectAbsent $RegistryProject
Assert-ProjectAbsent $AppProject

$EvidenceDirectory = Split-Path -Parent $Output
New-Item -ItemType Directory -Force -Path $EvidenceDirectory | Out-Null
$CurrentRegistryEvidence = Join-Path $EvidenceDirectory "registry-current.json"
$PreviousRegistryEvidence = Join-Path $EvidenceDirectory "registry-previous.json"
$CurrentDeploymentEvidence = Join-Path $EvidenceDirectory "registry-current-deployment.json"
$PreviousDeploymentEvidence = Join-Path $EvidenceDirectory "registry-rollback-deployment.json"
$CurrentQueueEvidence = Join-Path $EvidenceDirectory "registry-current-queue.json"
$PreviousQueueEvidence = Join-Path $EvidenceDirectory "registry-rollback-queue.json"

try {
    $CurrentSha = Get-ImageRevision $CurrentImage
    $PreviousSha = Get-ImageRevision $PreviousImage
    if ($CurrentSha -eq $PreviousSha) { throw "Current and previous images have the same revision" }
    $CurrentMigrations = @(Get-ImageMigrationInventory $CurrentImage)
    $PreviousMigrations = @(Get-ImageMigrationInventory $PreviousImage)
    $MigrationDifferences = @(
        Compare-Object -ReferenceObject $PreviousMigrations -DifferenceObject $CurrentMigrations
    )
    $MigrationInventoryIdentical = $MigrationDifferences.Count -eq 0
    $MigrationChangeDetected = -not $MigrationInventoryIdentical
    $CurrentMigrationInventorySha = Get-StringArraySha256 $CurrentMigrations
    $PreviousMigrationInventorySha = Get-StringArraySha256 $PreviousMigrations
    $DatabaseRollbackExercised = $false
    $DatabaseReleaseSafetyVerified = $MigrationInventoryIdentical
    $DatabaseReleaseBasis = "migration_inventory_identical"
    $DatabaseRollbackEvidenceSha256 = $null
    if ($MigrationChangeDetected) {
        if (-not $DatabaseRollbackEvidence) {
            throw "Migration inventory differs; database rollback rehearsal evidence is required before rollout"
        }
        $DatabaseRollbackEvidencePath = if ([System.IO.Path]::IsPathRooted($DatabaseRollbackEvidence)) {
            $DatabaseRollbackEvidence
        } else {
            Join-Path $Root $DatabaseRollbackEvidence
        }
        & $Python $DatabaseRollbackVerifier `
            --evidence $DatabaseRollbackEvidencePath `
            --expected-current-sha $CurrentSha `
            --expected-rollback-sha $PreviousSha `
            --expected-current-inventory-sha256 $CurrentMigrationInventorySha `
            --expected-rollback-inventory-sha256 $PreviousMigrationInventorySha `
            --expected-current-migration-count $CurrentMigrations.Count `
            --expected-rollback-migration-count $PreviousMigrations.Count
        Assert-ExitCode "Database rollback evidence verification failed"
        $DatabaseRollbackExercised = $true
        $DatabaseReleaseSafetyVerified = $true
        $DatabaseReleaseBasis = "snapshot_restore_rehearsed"
        $DatabaseRollbackEvidenceSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $DatabaseRollbackEvidencePath).Hash.ToLowerInvariant()
    }

    Write-Host "[1/6] Start loopback-only local Registry"
    $env:LOCAL_REGISTRY_PORT = [string]$RegistryPort
    docker compose -p $RegistryProject -f $RegistryCompose up -d
    Assert-ExitCode "Local Registry startup failed"
    $RegistryStarted = $true
    Wait-Registry

    Write-Host "[2/6] Push and force-pull current and previous images"
    $CurrentArtifact = Publish-Image $CurrentImage $CurrentSha "current-$($CurrentSha.Substring(0, 12))" $CurrentRegistryEvidence
    $PreviousArtifact = Publish-Image $PreviousImage $PreviousSha "previous-$($PreviousSha.Substring(0, 12))" $PreviousRegistryEvidence

    Write-Host "[3/6] Deploy current image by Registry digest"
    Start-Application $CurrentArtifact.digest_reference $CurrentSha
    Invoke-DeploymentProbe $CurrentArtifact.digest_reference $CurrentSha $CurrentDeploymentEvidence
    Invoke-QueueProbe $CurrentQueueEvidence

    Write-Host "[4/6] Roll back to previous Registry digest"
    Start-Application $PreviousArtifact.digest_reference $PreviousSha
    Invoke-DeploymentProbe $PreviousArtifact.digest_reference $PreviousSha $PreviousDeploymentEvidence
    Invoke-QueueProbe $PreviousQueueEvidence

    if ($RestoreCurrentBeforeExit) {
        Write-Host "[5/7] Restore current image for target-node verification"
        Start-Application $CurrentArtifact.digest_reference $CurrentSha
    }

    Write-Host ($(if ($RestoreCurrentBeforeExit) { "[6/7]" } else { "[5/6]" }) + " Write redacted rehearsal summary")
    $Summary = [ordered]@{
        schema_version = "registry-rollout-rollback-evidence-v1"
        generated_at = [DateTimeOffset]::UtcNow.ToString("o")
        executed = $true
        passed = $true
        registry_scope = "local_loopback"
        registry_endpoint_fingerprint = $CurrentArtifact.registry_endpoint_fingerprint
        current = [ordered]@{
            source_git_sha = $CurrentSha
            digest = $CurrentArtifact.digest
            digest_reference = $CurrentArtifact.digest_reference
            registry_published = $CurrentArtifact.registry_published
            registry_pull_verified = $CurrentArtifact.registry_pull_verified
            deployment_verified = $true
            queue_verified = $true
        }
        rollback = [ordered]@{
            source_git_sha = $PreviousSha
            digest = $PreviousArtifact.digest
            digest_reference = $PreviousArtifact.digest_reference
            registry_published = $PreviousArtifact.registry_published
            registry_pull_verified = $PreviousArtifact.registry_pull_verified
            deployment_verified = $true
            queue_verified = $true
        }
        database_migration_change_detected = $MigrationChangeDetected
        database_migration_rollback_required = $MigrationChangeDetected
        database_migration_rollback_exercised = $DatabaseRollbackExercised
        database_release_safety_verified = $DatabaseReleaseSafetyVerified
        database_release_basis = $DatabaseReleaseBasis
        database_rollback_evidence_sha256 = $DatabaseRollbackEvidenceSha256
        database_migration_inventory = [ordered]@{
            current_count = $CurrentMigrations.Count
            previous_count = $PreviousMigrations.Count
            current_sha256 = $CurrentMigrationInventorySha
            previous_sha256 = $PreviousMigrationInventorySha
            identical = $MigrationInventoryIdentical
        }
        current_restored_for_target_probe = [bool]$RestoreCurrentBeforeExit
        enterprise_registry_verified = $false
        cross_host_verified = $false
        production_ready = $false
        limitations = @(
            "loopback_registry_without_tls_or_auth",
            "single_docker_daemon",
            "stub_model",
            "mock_fact_adapters",
            $(if ($MigrationInventoryIdentical) {
                "database_migration_rollback_not_required_for_identical_inventory"
            } else {
                "database_snapshot_restore_rehearsed_on_disposable_postgres"
            })
        )
    }
    $Summary | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $Output -Encoding utf8
    Write-Host "Registry rollout and rollback rehearsal passed: $Output"
}
finally {
    Write-Host ($(if ($RestoreCurrentBeforeExit) { "[7/7]" } else { "[6/6]" }) + " Cleanup disposable rehearsal resources")
    if (-not $KeepResources) {
        if ($AppStarted) {
            docker compose -p $AppProject -f $BaseCompose --profile minio down -v --remove-orphans | Out-Null
        }
        if ($RegistryStarted) {
            docker compose -p $RegistryProject -f $RegistryCompose down -v --remove-orphans | Out-Null
        }
    }
    foreach ($name in $ManagedVariables) {
        [Environment]::SetEnvironmentVariable($name, $PreviousEnvironment[$name], "Process")
    }
}
