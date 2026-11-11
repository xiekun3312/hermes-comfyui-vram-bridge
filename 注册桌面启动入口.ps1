param([string]$PythonExe)

$ErrorActionPreference = 'Stop'

if (-not $PythonExe) {
    $PythonExe = Get-Command python.exe -CommandType Application | Select-Object -First 1 -ExpandProperty Source
}
$PythonExe = (Resolve-Path -LiteralPath $PythonExe).Path
$pythonw = Join-Path (Split-Path -Parent $PythonExe) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) {
    throw "Could not find pythonw.exe beside $PythonExe"
}
& $PythonExe -B -c 'import uiautomation'
if ($LASTEXITCODE -ne 0) {
    throw "Install requirements.txt with $PythonExe before registering the launcher."
}

$taskName = 'ComfyUI-VRAM-Bridge-Desktop'
$entry = Join-Path $PSScriptRoot 'bridge.py'
$arguments = '-X utf8 -B "{0}" serve --desktop-task' -f $entry
$existing = Get-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Count -ne 1 -or $existing.Actions[0].Arguments -ne $arguments)) {
    throw "The desktop launcher name is already registered for another entry: $taskName"
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$action = New-ScheduledTaskAction -Execute $pythonw -Argument $arguments -WorkingDirectory $PSScriptRoot
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -TaskPath '\' -Action $action -Principal $principal -Settings $settings `
    -Description 'Starts the ComfyUI VRAM bridge on demand in the signed-in Windows desktop session.' -Force | Out-Null
Write-Output "Registered $taskName for $identity (on demand)."
