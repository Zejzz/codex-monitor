$monitorScript = Join-Path $PSScriptRoot 'monitor.py'
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains('"' + $monitorScript + '"') } |
    ForEach-Object { Stop-Process -Id $_.ProcessId }
