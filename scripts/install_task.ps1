param(
    [switch]$Enable,
    [switch]$Disable
)

$TaskName = "McpHub"
$PythonExe = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
$Action = "$PythonExe -m mcp_hub serve"

if ($Disable) {
    schtasks /Delete /TN $TaskName /F
    Write-Host "Autostart disabled."
    return
}

if ($Enable) {
    schtasks /Create /TN $TaskName /TR "`"$PythonExe`" -m mcp_hub serve" /SC ONLOGON /F
    Write-Host "Autostart enabled: $TaskName will run at logon."
    return
}

Write-Host "Usage: install_task.ps1 -Enable | -Disable"
