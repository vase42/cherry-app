# Creates the Cherry shortcut with the cherry icon.
#
# Run it by double-clicking "Make desktop shortcut.bat" in the Cherry folder, or:
#
#   powershell -ExecutionPolicy Bypass -File system\make_shortcut.ps1
#
# Makes "Cherry.lnk" in the Cherry folder and, unless you pass -NoDesktop, one
# on the Desktop too. Double-clicking it starts the server and opens the app.
# A shortcut holds absolute paths, which is why it is made on the PC itself
# rather than shipped in the zip.
#
# Windows caches icons aggressively; if the old icon lingers, log out and back
# in, or run:  ie4uinit.exe -show

param([switch]$NoDesktop)

$system = Split-Path -Parent $MyInvocation.MyCommand.Path
$root   = Split-Path -Parent $system
$icon   = Join-Path $system "cherry.ico"
$bat    = Join-Path $root "Start Cherry.bat"

if (-not (Test-Path $icon)) { throw "system\cherry.ico is missing." }
if (-not (Test-Path $bat))  { throw "Start Cherry.bat is missing." }

function New-CherryShortcut([string]$path) {
    $shell = New-Object -ComObject WScript.Shell
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath       = $bat
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation     = "$icon,0"
    $lnk.Description      = "Cherry - prompts to images to video"
    $lnk.WindowStyle      = 7          # start minimised; the app opens a browser
    $lnk.Save()
    Write-Host "  created $path"
}

New-CherryShortcut (Join-Path $root "Cherry.lnk")
if (-not $NoDesktop) {
    New-CherryShortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "Cherry.lnk")
}

Write-Host ""
Write-Host "  Done. Double-click Cherry to start the app."
