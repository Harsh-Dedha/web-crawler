#requires -Version 5.1
<#
.SYNOPSIS
    Stop the portable PostgreSQL + Redis services started by start_infra.ps1.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$PgBin = Join-Path $Root "runtime\pgsql\pgsql\bin"
$PgData = Join-Path $Root "runtime\pgdata"
$RedisRoot = Join-Path $Root "runtime\redis\Redis-8.10.2-Windows-x64-msys2"

if (Test-Path -LiteralPath (Join-Path $PgData "PG_VERSION")) {
    Write-Host "Stopping PostgreSQL..."
    & (Join-Path $PgBin "pg_ctl.exe") -D $PgData -m fast -w stop
}

$redisCli = Join-Path $RedisRoot "redis-cli.exe"
if (Test-Path -LiteralPath $redisCli) {
    $pong = & $redisCli -h 127.0.0.1 -p 6379 ping 2>$null
    if ($pong -eq "PONG") {
        Write-Host "Stopping Redis..."
        & $redisCli -h 127.0.0.1 -p 6379 shutdown save 2>$null
    }
}

Write-Host "Services stopped."
