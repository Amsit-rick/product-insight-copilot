param(
    [ValidateSet("start", "status", "stop", "reset", "logs")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$ProjectName = "product-insight-commerce"
$Root = Split-Path -Parent $PSScriptRoot
$ComposeFile = Join-Path $Root "deploy\docker-compose.commerce-sandbox.yml"
$EnvFile = Join-Path $Root "deploy\.commerce-sandbox.env"

function New-RandomSecret([int]$Bytes = 32) {
    $buffer = New-Object byte[] $Bytes
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($buffer)
    }
    finally {
        $generator.Dispose()
    }
    return ([BitConverter]::ToString($buffer) -replace "-", "").ToLowerInvariant()
}

function Ensure-LocalEnvironment {
    if (Test-Path -LiteralPath $EnvFile) {
        return
    }
    $content = @(
        "SALEOR_SECRET_KEY=$(New-RandomSecret 48)"
        "SALEOR_SEED_ADMIN_EMAIL=admin@example.com"
        "SALEOR_SEED_ADMIN_PASSWORD=$(New-RandomSecret 24)"
        "COMMERCE_SANDBOX_PORT=8092"
        "SANDBOX_API_KEY=local-sandbox-key"
        "SANDBOX_WEBHOOK_SECRET=$(New-RandomSecret 32)"
    )
    [System.IO.File]::WriteAllLines(
        $EnvFile,
        $content,
        (New-Object System.Text.UTF8Encoding($false))
    )
    Write-Host "Created local-only environment file: $EnvFile"
}

function Invoke-CommerceCompose([string[]]$Arguments) {
    & docker compose `
        -p $ProjectName `
        --env-file $EnvFile `
        -f $ComposeFile `
        --profile commerce-sandbox `
        @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose failed with exit code $LASTEXITCODE"
    }
}

switch ($Action) {
    "start" {
        Ensure-LocalEnvironment
        Invoke-CommerceCompose @("up", "-d", "--build", "commerce-integration-sandbox")
        Invoke-CommerceCompose @("ps")
        Write-Host "Commerce facts: http://127.0.0.1:8092/health"
    }
    "status" {
        Ensure-LocalEnvironment
        Invoke-CommerceCompose @("ps", "-a")
    }
    "stop" {
        Ensure-LocalEnvironment
        Invoke-CommerceCompose @("stop")
    }
    "reset" {
        Ensure-LocalEnvironment
        Invoke-CommerceCompose @("down", "--volumes", "--remove-orphans")
        Write-Host "Only the dedicated $ProjectName containers and volumes were removed."
    }
    "logs" {
        Ensure-LocalEnvironment
        Invoke-CommerceCompose @("logs", "--tail", "200", "commerce-api", "commerce-scenarios", "commerce-integration-sandbox")
    }
}
