#requires -Version 5.1
<#
.SYNOPSIS
    End-to-end smoke test: seed, crawl, rebuild the index, query both engines.
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://127.0.0.1:8000",
    [int]$Workers = 6,
    [int]$TimeoutSeconds = 180
)

$ErrorActionPreference = "Stop"
$seeds = @(
    "https://en.wikipedia.org/wiki/Distributed_computing",
    "https://en.wikipedia.org/wiki/Search_engine"
)

Write-Host "Health check..."
Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get | ConvertTo-Json -Depth 5

Write-Host "Seeding frontier..."
$seedBody = ConvertTo-Json -InputObject $seeds -Compress
Invoke-RestMethod -Uri "$BaseUrl/crawler/seed" -Method Post -ContentType "application/json" -Body $seedBody |
    ConvertTo-Json -Depth 5

Write-Host "Starting crawler with $Workers workers..."
Invoke-RestMethod -Uri "$BaseUrl/crawler/start?workers=$Workers" -Method Post |
    ConvertTo-Json -Depth 5

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
do {
    Start-Sleep -Seconds 3
    $status = Invoke-RestMethod -Uri "$BaseUrl/crawler/status" -Method Get
    Write-Host ("  pages={0} queue={1} running={2}" -f $status.pages_in_database, $status.frontier_queue_length, $status.running)
} while ($status.running -and (Get-Date) -lt $deadline)

if ($status.running) {
    Write-Host "Timeout reached; stopping crawler..."
    Invoke-RestMethod -Uri "$BaseUrl/crawler/stop" -Method Post | Out-Null
}

Write-Host "Rebuilding BM25 index..."
Invoke-RestMethod -Uri "$BaseUrl/index/rebuild" -Method Post | ConvertTo-Json -Depth 5

foreach ($mode in @("bm25", "postgres")) {
    Write-Host "Searching mode=$mode..."
    $uri = "$BaseUrl/search?q=distributed%20computing%20networks&mode=$mode&limit=5"
    Invoke-RestMethod -Uri $uri -Method Get | ConvertTo-Json -Depth 8
}

Write-Host "Stats..."
Invoke-RestMethod -Uri "$BaseUrl/stats" -Method Get | ConvertTo-Json -Depth 8
