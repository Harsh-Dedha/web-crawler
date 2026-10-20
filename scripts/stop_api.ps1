#requires -Version 5.1
<#
.SYNOPSIS
    Stop the FastAPI server listening on port 8000.
#>
[CmdletBinding()]
param(
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$connections = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $connections) {
    Write-Host "No API is listening on port $Port."
    return
}

$processIds = $connections.OwningProcess | Sort-Object -Unique
foreach ($processId in $processIds) {
    $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($process) {
        Write-Host "Stopping $($process.ProcessName) (PID $processId)..."
        Stop-Process -Id $processId -Force
    }
}

for ($i = 0; $i -lt 20; $i++) {
    if (-not (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)) {
        Write-Host "API stopped."
        return
    }
    Start-Sleep -Milliseconds 250
}

throw "API process did not release port $Port."
