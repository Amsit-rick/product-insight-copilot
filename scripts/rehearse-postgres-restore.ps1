param(
    [string]$PythonPath = "backend\.tmp\clean-venv-20260930\Scripts\python.exe",
    [string]$ContainerName = "pic-postgres-restore-rehearsal",
    [int]$Port = 55433
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Python = if ([System.IO.Path]::IsPathRooted($PythonPath)) {
    $PythonPath
} else {
    Join-Path $Root $PythonPath
}
$Password = "pic_restore_rehearsal_only"
$SourceDatabase = "pic"
$RestoreDatabase = "pic_restore"
$Started = $false
$MigrationDirectory = Join-Path $Backend "migrations"
$ExpectedMigrationCount = @(Get-ChildItem -LiteralPath $MigrationDirectory -Filter "*.sql" -File).Count

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Python executable not found: $Python"
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "docker was not found"
}
if ($ExpectedMigrationCount -lt 1) {
    throw "No versioned migrations found in: $MigrationDirectory"
}

$existing = docker ps -a --filter "name=^/$ContainerName$" --format "{{.Names}}"
if ($existing) {
    throw "Refusing to replace existing container: $ContainerName"
}

try {
    Write-Host "[1/6] Start disposable PostgreSQL + pgvector"
    docker run -d --name $ContainerName `
        -e "POSTGRES_PASSWORD=$Password" `
        -e "POSTGRES_DB=$SourceDatabase" `
        -p "127.0.0.1:${Port}:5432" `
        pgvector/pgvector:pg16 | Out-Null
    $runExitCode = $LASTEXITCODE
    $created = docker ps -a --filter "name=^/$ContainerName$" --format "{{.Names}}"
    $Started = $created -eq $ContainerName
    if ($runExitCode -ne 0) { throw "Failed to start PostgreSQL container" }

    $ready = $false
    foreach ($attempt in 1..30) {
        docker exec $ContainerName pg_isready -U postgres -d $SourceDatabase | Out-Null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $ready) { throw "PostgreSQL was not ready within 30 seconds" }

    Write-Host "[2/6] Apply all versioned migrations"
    $previousDatabaseUrl = $env:DATABASE_URL
    $env:DATABASE_URL = "postgresql+psycopg://postgres:$Password@127.0.0.1:$Port/$SourceDatabase"
    Push-Location $Backend
    try {
        & $Python -m app.db.migrate apply
        if ($LASTEXITCODE -ne 0) { throw "Versioned migrations failed" }
    }
    finally {
        Pop-Location
        $env:DATABASE_URL = $previousDatabaseUrl
    }

    Write-Host "[3/6] Add restore probe and create a custom-format snapshot"
    docker exec $ContainerName psql -v ON_ERROR_STOP=1 -U postgres -d $SourceDatabase -c `
        "CREATE TABLE restore_probe (id integer PRIMARY KEY, value text NOT NULL); INSERT INTO restore_probe VALUES (1, 'before-backup');" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to insert restore probe" }
    docker exec $ContainerName pg_dump -U postgres -d $SourceDatabase -Fc -f /tmp/pic-before-release.dump
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed" }

    Write-Host "[4/6] Simulate source database damage"
    docker exec $ContainerName psql -v ON_ERROR_STOP=1 -U postgres -d $SourceDatabase -c `
        "DROP TABLE tickets CASCADE; DELETE FROM restore_probe;" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to simulate damage" }

    Write-Host "[5/6] Restore into an isolated database"
    docker exec $ContainerName createdb -U postgres $RestoreDatabase
    if ($LASTEXITCODE -ne 0) { throw "Failed to create restore target" }
    docker exec $ContainerName pg_restore --exit-on-error -U postgres -d $RestoreDatabase /tmp/pic-before-release.dump | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "pg_restore failed" }

    Write-Host "[6/6] Verify migration history, business table, and probe row"
    $migrationCount = (docker exec $ContainerName psql -At -U postgres -d $RestoreDatabase -c `
        "SELECT count(*) FROM schema_migrations;").Trim()
    $ticketTable = (docker exec $ContainerName psql -At -U postgres -d $RestoreDatabase -c `
        "SELECT to_regclass('public.tickets');").Trim()
    $probe = (docker exec $ContainerName psql -At -U postgres -d $RestoreDatabase -c `
        "SELECT value FROM restore_probe WHERE id = 1;").Trim()

    if ([int]$migrationCount -ne $ExpectedMigrationCount) {
        throw "Unexpected migration count after restore: $migrationCount (expected $ExpectedMigrationCount)"
    }
    if ($ticketTable -ne "tickets") { throw "tickets table missing after restore: $ticketTable" }
    if ($probe -ne "before-backup") { throw "Unexpected probe value after restore: $probe" }

    Write-Host "Restore rehearsal passed: $ExpectedMigrationCount migrations, tickets table, and probe row restored."
}
finally {
    if ($Started) {
        docker rm -f $ContainerName | Out-Null
    }
}
