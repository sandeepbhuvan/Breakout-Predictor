# Creates a "Breakout Predictor" shortcut on the desktop that launches the app without a console window.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'breakout_predictor_app.py'

$python = (& py -c "import sys; print(sys.executable)").Trim()
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found next to $python" }

$icon = Join-Path $env:USERPROFILE '.breakout_predictor\icon.ico'
& $python $script --make-icon $icon

$lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Breakout Predictor.lnk'
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "`"$script`""
$shortcut.WorkingDirectory = $here
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = 'Nifty 500 breakout predictor (Upstox)'
$shortcut.Save()
Write-Host "Shortcut created: $lnk"
