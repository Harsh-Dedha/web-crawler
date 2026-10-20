#requires -Version 5.1
<#
.SYNOPSIS
    Start the portable PostgreSQL + Redis services used by this project.
.DESCRIPTION
    Intended for the native-Windows fallback when Docker is unavailable.
    The binaries are expected under .\runtime\ (see README).
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$PgBin = Join-Path $Root "runtime\pgsql\pgsql\bin"
$PgData = Join-Path $Root "runtime\pgdata"
$RedisRoot = Join-Path $Root "runtime\redis\Redis-8.10.2-Windows-x64-msys2"
$RedisData = Join-Path $Root "runtime\redis-data"
$LogDir = Join-Path $Root "logs"

foreach ($path in @($PgBin, $RedisRoot)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Missing runtime directory: $path. Install Docker and use docker compose, or place the portable binaries under .\runtime\."
    }
}
New-Item -ItemType Directory -Path $PgData, $RedisData, $LogDir -Force | Out-Null

# ---------------------------------------------------------------- Redis ----
$redisConf = Join-Path $RedisRoot "redis-local.conf"
@'
bind 127.0.0.1
port 6379
protected-mode yes
appendonly yes
appendfilename "appendonly.aof"
dir ../../redis-data
save 900 1
save 300 10
logfile ""
'@ | Set-Content -LiteralPath $redisConf -Encoding ascii

$redisListening = Get-NetTCPConnection -LocalPort 6379 -State Listen -ErrorAction SilentlyContinue
if (-not $redisListening) {
    Write-Host "Starting Redis..."
    $redis = Start-Process -FilePath (Join-Path $RedisRoot "redis-server.exe") `
        -ArgumentList "redis-local.conf" `
        -WorkingDirectory $RedisRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir "redis.out.log") `
        -RedirectStandardError (Join-Path $LogDir "redis.err.log") `
        -PassThru
    Set-Content -LiteralPath (Join-Path $Root "runtime\redis.pid") -Value $redis.Id
}
else {
    Write-Host "Redis is already listening on 6379."
}

$redisCli = Join-Path $RedisRoot "redis-cli.exe"
$redisReady = $false
for ($i = 0; $i -lt 20; $i++) {
    if ((& $redisCli -h 127.0.0.1 -p 6379 ping 2>$null) -eq "PONG") {
        $redisReady = $true
        break
    }
    Start-Sleep -Milliseconds 500
}
if (-not $redisReady) { throw "Redis did not become ready. See logs\redis.err.log." }
Write-Host "Redis: PONG"

# ------------------------------------------------------------- Postgres ----
$pgVersionFile = Join-Path $PgData "PG_VERSION"
if (-not (Test-Path -LiteralPath $pgVersionFile)) {
    if (Test-Path -LiteralPath $PgData) {
        $existing = Get-ChildItem -LiteralPath $PgData -Force -ErrorAction SilentlyContinue
        if ($existing) {
            throw "$PgData exists but is not a valid PostgreSQL data directory. Remove it and rerun."
        }
    }
    Write-Host "Initializing PostgreSQL data directory..."
    & (Join-Path $PgBin "initdb.exe") -D $PgData -U crawler_user --auth=trust --encoding=UTF8 --locale=C
    if ($LASTEXITCODE -ne 0) { throw "initdb failed with exit code $LASTEXITCODE" }
}

$pgListening = Get-NetTCPConnection -LocalPort 5432 -State Listen -ErrorAction SilentlyContinue
if (-not $pgListening) {
    Write-Host "Starting PostgreSQL..."
    & (Join-Path $PgBin "pg_ctl.exe") -D $PgData `
        -l (Join-Path $LogDir "postgres.log") `
        -o "-p 5432 -c listen_addresses=127.0.0.1" `
        -w start
    if ($LASTEXITCODE -ne 0) { throw "pg_ctl failed with exit code $LASTEXITCODE" }
}
else {
    Write-Host "PostgreSQL is already listening on 5432."
}

$psql = Join-Path $PgBin "psql.exe"
$env:PGPASSWORD = "crawler_password"
$dbExists = (& $psql -h 127.0.0.1 -p 5432 -U crawler_user -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = 'search_engine';" 2>$null)
if ($dbExists -ne "1") {
    Write-Host "Creating database search_engine..."
    & (Join-Path $PgBin "createdb.exe") -h 127.0.0.1 -p 5432 -U crawler_user search_engine
    if ($LASTEXITCODE -ne 0) { throw "createdb failed with exit code $LASTEXITCODE" }
}

& $psql -h 127.0.0.1 -p 5432 -U crawler_user -d search_engine -c "ALTER USER crawler_user WITH PASSWORD 'crawler_password';" | Out-Null
Write-Host "Applying schema.sql..."
& $psql -h 127.0.0.1 -p 5432 -U crawler_user -d search_engine -v ON_ERROR_STOP=1 -f (Join-Path $Root "schema.sql")
if ($LASTEXITCODE -ne 0) { throw "schema.sql failed with exit code $LASTEXITCODE" }

Write-Host ""
Write-Host "Infrastructure ready:"
Write-Host "  PostgreSQL  postgresql://crawler_user:crawler_password@127.0.0.1:5432/search_engine"
Write-Host "  Redis       redis://127.0.0.1:6379/0"
