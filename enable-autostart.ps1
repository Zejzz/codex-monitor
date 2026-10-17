$ErrorActionPreference = 'Stop'
$monitorLoginShortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'Codex Monitor.lnk'
$monitorLoginPython = (Get-Command python.exe -ErrorAction Stop).Source
$monitorLoginPowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$monitorLoginShell = New-Object -ComObject WScript.Shell
$monitorLoginLink = $monitorLoginShell.CreateShortcut($monitorLoginShortcut)
$monitorLoginLink.TargetPath = $monitorLoginPowerShell
$monitorLoginLink.Arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'launch.ps1') + '" -NoBrowser -PythonPath "' + $monitorLoginPython + '"'
$monitorLoginLink.WorkingDirectory = $PSScriptRoot
$monitorLoginLink.WindowStyle = 7
$monitorLoginLink.Description = 'Start Codex Monitor quietly at Windows sign-in.'
$monitorLoginLink.Save()
Write-Output "Autostart installed: $monitorLoginShortcut"
