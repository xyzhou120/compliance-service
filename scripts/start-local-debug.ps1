param(
    [string]$Config = "",
    [string]$EnvironmentFile = ".local/local-debug.env.json",
    [int]$Port = 8091,
    [string]$Database = ".local/debug.sqlite3",
    [switch]$SkipFrontendBuild
)
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$previousEnvironment = @{}
$workerProcess = $null
$apiProcess = $null
$previousWorkerMode = [Environment]::GetEnvironmentVariable("COMPLIANCE_DEBUG_EXTERNAL_WORKER", "Process")
Push-Location -LiteralPath $projectRoot
try {
    if (-not $Config -and (Test-Path -LiteralPath "config/local-debug.toml")) {
        $Config = "config/local-debug.toml"
    }
    if ($Config -and $EnvironmentFile -and (Test-Path -LiteralPath $EnvironmentFile)) {
        try { $localEnvironment = Get-Content -LiteralPath $EnvironmentFile -Raw | ConvertFrom-Json }
        catch { throw "Local database environment file must contain valid JSON" }
        if ($null -eq $localEnvironment -or $localEnvironment -isnot [pscustomobject]) {
            throw "Local database environment file must contain a JSON object"
        }
        foreach ($entry in $localEnvironment.PSObject.Properties) {
            if ($entry.Name -notmatch '^COMPLIANCE_OB_(HOST|PORT|DATABASE|USER|PASSWORD)$' -or
                $entry.Value -isnot [string] -or [string]::IsNullOrWhiteSpace($entry.Value)) {
                throw "Local database environment file contains an invalid variable"
            }
        }
        foreach ($entry in $localEnvironment.PSObject.Properties) {
            # Explicit process environment takes precedence over local defaults.
            if (-not [Environment]::GetEnvironmentVariable($entry.Name, "Process")) {
                $previousEnvironment[$entry.Name] = [Environment]::GetEnvironmentVariable($entry.Name, "Process")
                [Environment]::SetEnvironmentVariable($entry.Name, $entry.Value, "Process")
            }
        }
    }
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
    $frontendRoot = Join-Path $projectRoot "frontend"
    $frontendIndex = Join-Path $projectRoot "compliance_service\web\index.html"
    if ((Test-Path -LiteralPath (Join-Path $frontendRoot "package.json")) -and -not $SkipFrontendBuild) {
        $buildNeeded = -not (Test-Path -LiteralPath $frontendIndex)
        if (-not $buildNeeded) {
            $builtAt = (Get-Item -LiteralPath $frontendIndex).LastWriteTimeUtc
            $frontendSources = @(Get-ChildItem -LiteralPath (Join-Path $frontendRoot "src") -File -Recurse)
            $frontendSources += @(Get-ChildItem -LiteralPath $frontendRoot -File | Where-Object { $_.Name -match '^(index\.html|package(-lock)?\.json|vite\.config\.ts|tsconfig\.json)$' })
            $buildNeeded = @($frontendSources | Where-Object { $_.LastWriteTimeUtc -gt $builtAt }).Count -gt 0
        }
        if ($buildNeeded) {
            $npmPath = (Get-Command npm.cmd -ErrorAction Stop).Source
            Push-Location -LiteralPath $frontendRoot
            try {
                if (-not (Test-Path -LiteralPath "node_modules\.package-lock.json")) {
                    if (Test-Path -LiteralPath "package-lock.json") { & $npmPath ci --no-audit --no-fund }
                    else { & $npmPath install --no-audit --no-fund }
                    if ($LASTEXITCODE -ne 0) { throw "Frontend dependencies could not be installed" }
                }
                & $npmPath run build
                if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
            } finally { Pop-Location }
        }
    }
    $arguments = @("-m", "compliance_service", "--db", $Database)
    if ($Config) {
        $arguments += @("--production-config", $Config)
        Write-Host "Adapter configuration: $Config"
    }
    $occupied = Get-NetTCPConnection -LocalAddress "127.0.0.1" -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($occupied) { throw "Port $Port is already in use. Existing debug page: http://127.0.0.1:$Port/debug" }
    $arguments += @("serve", "--host", "127.0.0.1", "--port", "$Port")
    $workerArguments = @("-m", "compliance_service", "--db", $Database)
    if ($Config) {
        $workerArguments += @("--production-config", $Config)
    }
    $workerArguments += @("worker", "--debug-only")
    [Environment]::SetEnvironmentVariable("COMPLIANCE_DEBUG_EXTERNAL_WORKER", "1", "Process")
    $workerProcess = Start-Process -FilePath $pythonPath -ArgumentList $workerArguments `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput ".local/debug-worker.stdout.log" `
        -RedirectStandardError ".local/debug-worker.stderr.log"
    Start-Sleep -Milliseconds 600
    if ($workerProcess.HasExited) { throw "Local debug worker exited during startup" }
    $apiArguments = @("-m", "compliance_service", "--db", $Database)
    if ($Config) { $apiArguments += @("--production-config", $Config) }
    $apiArguments += @("serve", "--host", "127.0.0.1", "--port", "$Port")
    $apiProcess = Start-Process -FilePath $pythonPath -ArgumentList $apiArguments `
        -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput ".local/debug-api.stdout.log" `
        -RedirectStandardError ".local/debug-api.stderr.log"
    Write-Host "Local debug page: http://127.0.0.1:$Port/debug"
    Write-Host "Local debug worker PID: $($workerProcess.Id). Tasks execute automatically; no callback is sent."
    while ($true) {
        Start-Sleep -Seconds 1
        $workerProcess.Refresh()
        $apiProcess.Refresh()
        if ($workerProcess.HasExited) { throw "Local debug worker exited with code $($workerProcess.ExitCode)" }
        if ($apiProcess.HasExited) { throw "Local debug API exited with code $($apiProcess.ExitCode)" }
    }
} finally {
    if ($apiProcess -and -not $apiProcess.HasExited) {
        Stop-Process -Id $apiProcess.Id -ErrorAction SilentlyContinue
    }
    if ($workerProcess -and -not $workerProcess.HasExited) {
        Stop-Process -Id $workerProcess.Id -ErrorAction SilentlyContinue
    }
    [Environment]::SetEnvironmentVariable("COMPLIANCE_DEBUG_EXTERNAL_WORKER", $previousWorkerMode, "Process")
    foreach ($name in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], "Process")
    }
    Pop-Location
}
