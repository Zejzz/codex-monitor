param([switch]$NoBrowser, [string]$PythonPath)
$ErrorActionPreference = 'Stop'
$monitorRoot = $PSScriptRoot
$monitorUrl = 'http://127.0.0.1:8765'
$monitorRunning = $false
try {
    $monitorResponse = Invoke-WebRequest -Uri "$monitorUrl/" -TimeoutSec 2 -UseBasicParsing
    $monitorRunning = $monitorResponse.StatusCode -eq 200 -and $monitorResponse.Content.Contains('<title>Codex Monitor</title>')
} catch {}
if (-not $monitorRunning) {
    $monitorPython = if ($PythonPath) { $PythonPath } else { (Get-Command python.exe -ErrorAction Stop).Source }
    $monitorScript = Join-Path $monitorRoot 'monitor.py'
    $monitorExisting = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains('"' + $monitorScript + '"') }
    if (-not $monitorExisting) {
        Start-Process -FilePath $monitorPython -ArgumentList @('"' + $monitorScript + '"') -WorkingDirectory $monitorRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $monitorRoot 'server.log') -RedirectStandardError (Join-Path $monitorRoot 'server-error.log')
    }
    for ($monitorAttempt = 0; $monitorAttempt -lt 30; $monitorAttempt++) {
        Start-Sleep -Milliseconds 200
        try {
            $monitorResponse = Invoke-WebRequest -Uri "$monitorUrl/" -TimeoutSec 1 -UseBasicParsing
            if ($monitorResponse.StatusCode -eq 200 -and $monitorResponse.Content.Contains('<title>Codex Monitor</title>')) { $monitorRunning = $true; break }
        } catch {}
    }
}
if (-not $monitorRunning) { throw 'Monitor could not start. See server-error.log in this folder.' }
if (-not $NoBrowser) { Start-Process $monitorUrl }
