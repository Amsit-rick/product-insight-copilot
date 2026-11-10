param(
    [string]$PythonPath = ".venv\Scripts\python.exe",
    [Parameter(Mandatory = $true)][string]$CurrentImage,
    [Parameter(Mandatory = $true)][string]$RollbackImage,
    [string]$ContainerName = "pic-database-migration-rollback",
    [int]$Port = 55434,
    [string]$JsonOut = ".tmp\release\database-migration-rollback.json",
    [switch]$KeepResources
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Python = if ([System.IO.Path]::IsPathRooted($PythonPath)) {
    $PythonPath
} else {
    Join-Path $Root $PythonPath
}
$Output = if ([System.IO.Path]::IsPathRooted($JsonOut)) {
    $JsonOut
} else {
    Join-Path $Root $JsonOut
}
$WorkDir = Join-Path $Root (".tmp\database-migration-rollback-" + [guid]::NewGuid().ToString("N"))
$ExpectedWorkPrefix = Join-Path $Root ".tmp\database-migration-rollback-"
$RollbackMigrations = Join-Path $WorkDir "rollback-migrations"
$CurrentMigrations = Join-Path $WorkDir "current-migrations"
$SourceDatabase = "pic"
$RestoreDatabase = "pic_restore"
$PostgresImage = "pgvector/pgvector:pg16@sha256:ccc6e83d6e35e931dc7c5def2022729d5a6c370318d099181995567ff1fb4d6b"
$ContainerCreated = $false
$PreviousDatabaseUrl = $env:DATABASE_URL

function Assert-ExitCode([string]$Message) {
    if ($LASTEXITCODE -ne 0) { throw "$Message (exit=$LASTEXITCODE)" }
}

function Get-ImageRevision([string]$Image) {
    $raw = docker image inspect $Image | Out-String
    Assert-ExitCode "Cannot inspect image $Image"
    $payloads = @($raw | ConvertFrom-Json)
    if ($payloads.Count -ne 1) { throw "Image inspection must return exactly one image: $Image" }
    $label = $payloads[0].Config.Labels.PSObject.Properties["org.opencontainers.image.revision"]
    $revision = if ($null -eq $label) { "" } else { [string]$label.Value }
    if ($revision.Trim() -notmatch '^[0-9a-f]{40}$') {
        throw "Image $Image does not contain a valid OCI revision label"
    }
    return $revision.Trim()
}

function Export-ImageMigrations([string]$Image, [string]$Destination) {
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $copyContainer = (docker create --entrypoint sh $Image -c "true").Trim()
    Assert-ExitCode "Cannot create migration copy container for $Image"
    try {
        docker cp "${copyContainer}:/app/migrations/." $Destination
        Assert-ExitCode "Cannot copy migrations from $Image"
    }
    finally {
        docker rm -f $copyContainer | Out-Null
    }
}

function Get-MigrationInventory([string]$Directory) {
    return @(
        Get-ChildItem -LiteralPath $Directory -Filter "*.sql" -File |
            Sort-Object Name |
            Select-Object -ExpandProperty Name
    )
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

function Get-DatabaseSchemaInventory([string]$Database) {
    $query = @'
SELECT item
FROM (
    SELECT format('table|%s|%s', c.relname, c.relkind) AS item
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
    UNION ALL
    SELECT format(
        'column|%s|%s|%s|%s|%s|%s',
        table_name,
        ordinal_position,
        column_name,
        data_type,
        is_nullable,
        COALESCE(column_default, '')
    ) AS item
    FROM information_schema.columns
    WHERE table_schema = 'public'
    UNION ALL
    SELECT format(
        'constraint|%s|%s|%s',
        conrelid::regclass::text,
        conname,
        pg_get_constraintdef(oid, true)
    ) AS item
    FROM pg_constraint
    WHERE connamespace = 'public'::regnamespace
    UNION ALL
    SELECT format('index|%s|%s|%s', tablename, indexname, indexdef) AS item
    FROM pg_indexes
    WHERE schemaname = 'public'
) inventory
ORDER BY item;
'@
    $rows = @(docker exec $ContainerName psql -At -U postgres -d $Database -c $query)
    Assert-ExitCode "Failed to inspect database schema inventory for $Database"
    return @($rows | ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ })
}

function Invoke-Migrations([string]$Directory) {
    Push-Location $Backend
    try {
        & $Python -m app.db.migrate --directory $Directory apply
        Assert-ExitCode "Versioned migrations failed for $Directory"
    }
    finally {
        Pop-Location
    }
}

if (-not (Test-Path -LiteralPath $Python)) { throw "Python executable not found: $Python" }
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "docker was not found" }
$existing = docker ps -a --filter "name=^/$ContainerName$" --format "{{.Names}}"
if ($existing) { throw "Refusing to replace existing container: $ContainerName" }

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
try {
    $CurrentSha = Get-ImageRevision $CurrentImage
    $RollbackSha = Get-ImageRevision $RollbackImage
    if ($CurrentSha -eq $RollbackSha) { throw "Current and rollback images must have different revisions" }

    Export-ImageMigrations $RollbackImage $RollbackMigrations
    Export-ImageMigrations $CurrentImage $CurrentMigrations
    $RollbackInventory = @(Get-MigrationInventory $RollbackMigrations)
    $CurrentInventory = @(Get-MigrationInventory $CurrentMigrations)
    if ($RollbackInventory.Count -lt 1 -or $CurrentInventory.Count -lt 1) {
        throw "Both images must contain versioned migrations"
    }
    if ((Compare-Object $RollbackInventory $CurrentInventory).Count -eq 0) {
        throw "Migration inventories must differ for rollback rehearsal"
    }
    $RollbackInventorySha = Get-StringArraySha256 $RollbackInventory
    $CurrentInventorySha = Get-StringArraySha256 $CurrentInventory

    $PasswordBytes = New-Object byte[] 24
    $Random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $Random.GetBytes($PasswordBytes) } finally { $Random.Dispose() }
    $Password = ([System.BitConverter]::ToString($PasswordBytes)).Replace("-", "").ToLowerInvariant()

    Write-Host "[1/7] Start disposable PostgreSQL"
    docker run -d --name $ContainerName `
        -e "POSTGRES_PASSWORD=$Password" `
        -e "POSTGRES_DB=$SourceDatabase" `
        -p "127.0.0.1:${Port}:5432" `
        $PostgresImage | Out-Null
    $RunExitCode = $LASTEXITCODE
    $created = docker ps -a --filter "name=^/$ContainerName$" --format "{{.Names}}"
    $ContainerCreated = $created -eq $ContainerName
    if ($RunExitCode -ne 0) { throw "Failed to start PostgreSQL container (exit=$RunExitCode)" }
    if (-not $ContainerCreated) { throw "PostgreSQL container was not created" }
    $ready = $false
    foreach ($attempt in 1..45) {
        docker exec $ContainerName pg_isready -U postgres -d $SourceDatabase | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw "PostgreSQL was not ready within 45 seconds" }
    $env:DATABASE_URL = "postgresql+psycopg://postgres:$Password@127.0.0.1:$Port/$SourceDatabase"

    Write-Host "[2/7] Apply rollback-image migration baseline"
    Invoke-Migrations $RollbackMigrations
    docker exec $ContainerName psql -v ON_ERROR_STOP=1 -U postgres -d $SourceDatabase -c `
        "CREATE TABLE restore_probe (id integer PRIMARY KEY, value text NOT NULL); INSERT INTO restore_probe VALUES (1, 'before-migration');" | Out-Null
    Assert-ExitCode "Failed to create pre-migration business probe"
    $BaselineSchemaSha = Get-StringArraySha256 @(Get-DatabaseSchemaInventory $SourceDatabase)

    Write-Host "[3/7] Create pre-release custom-format snapshot"
    $SnapshotStarted = [DateTimeOffset]::UtcNow
    docker exec $ContainerName pg_dump -U postgres -d $SourceDatabase -Fc -f /tmp/pic-before-release.dump
    Assert-ExitCode "pg_dump failed"
    $SnapshotCompleted = [DateTimeOffset]::UtcNow
    $SnapshotHashLine = (docker exec $ContainerName sha256sum /tmp/pic-before-release.dump).Trim()
    Assert-ExitCode "Failed to hash database snapshot"
    $SnapshotSha = ($SnapshotHashLine -split '\s+')[0].Trim().ToLowerInvariant()
    if ($SnapshotSha -notmatch '^[0-9a-f]{64}$') { throw "Invalid database snapshot SHA-256" }
    $SnapshotSize = [int64](docker exec $ContainerName stat -c %s /tmp/pic-before-release.dump).Trim()
    Assert-ExitCode "Failed to measure database snapshot"
    if ($SnapshotSize -lt 1) { throw "Database snapshot is empty" }

    Write-Host "[4/7] Apply current-image migration and post-release writes"
    Invoke-Migrations $CurrentMigrations
    $CurrentMigrationCount = [int](docker exec $ContainerName psql -At -U postgres -d $SourceDatabase -c "SELECT count(*) FROM schema_migrations;").Trim()
    if ($CurrentMigrationCount -ne $CurrentInventory.Count) { throw "Candidate migration history count mismatch" }
    $CandidateSchemaSha = Get-StringArraySha256 @(Get-DatabaseSchemaInventory $SourceDatabase)
    if ($CandidateSchemaSha -eq $BaselineSchemaSha) { throw "Candidate migration did not change the database schema" }
    docker exec $ContainerName psql -v ON_ERROR_STOP=1 -U postgres -d $SourceDatabase -c `
        "UPDATE restore_probe SET value = 'after-migration' WHERE id = 1;" | Out-Null
    Assert-ExitCode "Failed to create post-migration writes"
    $PostMigrationValue = (docker exec $ContainerName psql -At -U postgres -d $SourceDatabase -c "SELECT value FROM restore_probe WHERE id = 1;").Trim()
    if ($PostMigrationValue -ne "after-migration") { throw "Post-migration write was not observed" }
    $FailureSimulated = [DateTimeOffset]::UtcNow

    Write-Host "[5/7] Restore snapshot into isolated rollback database"
    $RestoreStarted = [DateTimeOffset]::UtcNow
    docker exec $ContainerName createdb -U postgres $RestoreDatabase
    Assert-ExitCode "Failed to create restore target"
    docker exec $ContainerName pg_restore --exit-on-error -U postgres -d $RestoreDatabase /tmp/pic-before-release.dump | Out-Null
    Assert-ExitCode "pg_restore failed"
    $RestoreCompleted = [DateTimeOffset]::UtcNow
    $ObservedRpoSeconds = ($FailureSimulated - $SnapshotCompleted).TotalSeconds
    $ObservedRtoSeconds = ($RestoreCompleted - $RestoreStarted).TotalSeconds

    Write-Host "[6/7] Verify migration history, schema rollback, and business data"
    $RestoredMigrationCount = [int](docker exec $ContainerName psql -At -U postgres -d $RestoreDatabase -c "SELECT count(*) FROM schema_migrations;").Trim()
    $RestoredSchemaSha = Get-StringArraySha256 @(Get-DatabaseSchemaInventory $RestoreDatabase)
    $RestoredTickets = (docker exec $ContainerName psql -At -U postgres -d $RestoreDatabase -c "SELECT to_regclass('public.tickets');").Trim()
    $RestoredProbe = (docker exec $ContainerName psql -At -U postgres -d $RestoreDatabase -c "SELECT value FROM restore_probe WHERE id = 1;").Trim()
    if ($RestoredMigrationCount -ne $RollbackInventory.Count) { throw "Rollback migration history count mismatch" }
    if ($RestoredSchemaSha -ne $BaselineSchemaSha) { throw "Restored schema does not match the pre-release baseline" }
    if ($RestoredTickets -ne "tickets") { throw "Business table missing after restore" }
    if ($RestoredProbe -ne "before-migration") { throw "Business probe was not restored to snapshot value" }

    Write-Host "[7/7] Write redacted rollback evidence"
    $Evidence = [ordered]@{
        schema_version = "database-migration-rollback-rehearsal-v1"
        generated_at = [DateTimeOffset]::UtcNow.ToString("o")
        executed = $true
        passed = $true
        current = [ordered]@{
            source_git_sha = $CurrentSha
            migration_count = $CurrentInventory.Count
            migration_inventory_sha256 = $CurrentInventorySha
        }
        rollback = [ordered]@{
            source_git_sha = $RollbackSha
            migration_count = $RollbackInventory.Count
            migration_inventory_sha256 = $RollbackInventorySha
        }
        snapshot_created = $true
        snapshot_format = "postgres_custom"
        snapshot_sha256 = $SnapshotSha
        snapshot_size_bytes = $SnapshotSize
        snapshot_started_at = $SnapshotStarted.ToString("o")
        snapshot_completed_at = $SnapshotCompleted.ToString("o")
        failure_simulated_at = $FailureSimulated.ToString("o")
        restore_started_at = $RestoreStarted.ToString("o")
        restore_completed_at = $RestoreCompleted.ToString("o")
        observed_rpo_seconds = [Math]::Round($ObservedRpoSeconds, 3)
        observed_rto_seconds = [Math]::Round($ObservedRtoSeconds, 3)
        restore_target_isolated = $true
        candidate_migration_applied = $true
        schema_change_verified = $true
        post_migration_write_verified = $true
        post_snapshot_writes_discarded = $true
        baseline_schema_sha256 = $BaselineSchemaSha
        candidate_schema_sha256 = $CandidateSchemaSha
        restored_schema_sha256 = $RestoredSchemaSha
        restore_verified = $true
        restore_duration_seconds = [Math]::Round($ObservedRtoSeconds, 3)
        migration_history_restored = $true
        business_data_restored = $true
        candidate_schema_absent_after_restore = $true
        database_migration_change_detected = $true
        database_migration_rollback_required = $true
        database_migration_rollback_exercised = $true
        database_release_safety_verified = $true
        database_release_basis = "snapshot_restore_rehearsed"
        backup_storage_scope = "container_local_ephemeral"
        backup_encrypted = $false
        key_management_verified = $false
        offsite_copy_verified = $false
        retention_policy_enforced = $false
        restore_access_audited = $false
        production_ready = $false
        limitations = @(
            "single_host_disposable_postgres",
            "isolated_restore_target",
            "rehearsal_data_volume",
            "no_production_rpo_rto_claim"
        )
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Output) | Out-Null
    $Evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Output -Encoding utf8
    Write-Host "Database migration rollback rehearsal passed: $Output"
}
finally {
    $env:DATABASE_URL = $PreviousDatabaseUrl
    if ($ContainerCreated -and -not $KeepResources) {
        docker rm -f $ContainerName | Out-Null
    }
    if (Test-Path -LiteralPath $WorkDir) {
        $ResolvedWorkDir = (Resolve-Path -LiteralPath $WorkDir).Path
        if (-not $ResolvedWorkDir.StartsWith($ExpectedWorkPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove unexpected migration rehearsal directory"
        }
        Remove-Item -LiteralPath $ResolvedWorkDir -Recurse -Force
    }
}

