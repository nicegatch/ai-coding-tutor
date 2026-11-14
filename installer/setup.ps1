# AI Tutor - one-click setup (started by setup.bat).
# Installs everything AI Tutor needs, picks the best AI model for this PC,
# makes it start with Windows and starts it. Safe to run again (it repairs /
# updates an existing install and keeps your settings and mistakes journal).

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Src      = Split-Path -Parent $PSScriptRoot           # the downloaded folder
$Dest     = Join-Path $env:LOCALAPPDATA 'AI-Tutor'      # where AI Tutor lives
$Startup  = [Environment]::GetFolderPath('Startup')
$Programs = [Environment]::GetFolderPath('Programs')
$PyVersion = '3.12.10'
$PyUrl     = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-amd64.exe"
$OllamaUrl = 'https://ollama.com/download/OllamaSetup.exe'

function Step($n, $text) { Write-Host ""; Write-Host "  [$n/6] $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "        $text" -ForegroundColor Green }
function Info($text) { Write-Host "        $text" }
function Fail($text) {
    Write-Host ""
    Write-Host "  Setup could not finish:" -ForegroundColor Red
    Write-Host "  $text" -ForegroundColor Red
    exit 1
}

function Download($url, $file, $what) {
    Info "Downloading $what ..."
    try {
        Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing
    } catch {
        try { Start-BitsTransfer -Source $url -Destination $file } catch {
            Fail "Could not download $what. Check your internet connection and run setup.bat again.`n  ($url)"
        }
    }
}

function Find-Python {
    # Returns the full path of a usable python.exe (3.9 or newer), or $null.
    $candidates = @()
    foreach ($cmd in @('py', 'python')) {
        $c = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($c -and $c.Source -notlike '*WindowsApps*') { $candidates += $c.Source }
    }
    $candidates += Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue | ForEach-Object FullName
    $candidates += Get-ChildItem "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue | ForEach-Object FullName
    $best = $null; $bestVer = [version]'0.0'
    foreach ($exe in $candidates | Select-Object -Unique) {
        try {
            $pyArgs = @('-c', 'import sys; print(sys.executable); print(sys.version_info[0], sys.version_info[1], sep=chr(46))')
            if ((Split-Path -Leaf $exe) -eq 'py.exe') { $pyArgs = @('-3') + $pyArgs }
            $out = & $exe @pyArgs 2>$null
            if ($LASTEXITCODE -eq 0 -and $out.Count -ge 2) {
                $ver = [version]$out[1]
                if ($ver -ge [version]'3.9' -and $ver -gt $bestVer -and (Test-Path $out[0])) { $best = $out[0]; $bestVer = $ver }
            }
        } catch { }
    }
    return $best
}

function Find-Ollama {
    $c = Get-Command ollama -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    foreach ($p in @("$env:LOCALAPPDATA\Programs\Ollama\ollama.exe", "$env:ProgramFiles\Ollama\ollama.exe")) {
        if (Test-Path $p) { return $p }
    }
    return $null
}

function Stop-Tutor {
    foreach ($pidFile in @("$Dest\logs\tutor.pid", "$Src\logs\tutor.pid")) {
        if (Test-Path $pidFile) {
            $id = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
            if ($id) { Stop-Process -Id ([int]$id) -Force -ErrorAction SilentlyContinue }
            Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Milliseconds 500
}

function New-Shortcut($path, $target, $arguments, $workdir, $description) {
    $sh = New-Object -ComObject WScript.Shell
    $lnk = $sh.CreateShortcut($path)
    $lnk.TargetPath = $target
    $lnk.Arguments = $arguments
    $lnk.WorkingDirectory = $workdir
    $lnk.Description = $description
    $lnk.Save()
}

Clear-Host
Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "            AI Tutor  -  one-time setup" -ForegroundColor Cyan
Write-Host "  ==========================================" -ForegroundColor Cyan
Write-Host "  This takes 5-30 minutes (mostly downloading the AI model)."
Write-Host "  You don't need to click anything. Just leave this window open."

if (-not [Environment]::Is64BitOperatingSystem) { Fail "AI Tutor needs 64-bit Windows 10 or 11." }

# ---------------------------------------------------------------- 1. Python
Step 1 "Python (the language AI Tutor is written in)"
$Python = Find-Python
if ($Python) {
    Ok "Found: $Python"
} else {
    $installer = Join-Path $env:TEMP "python-$PyVersion-amd64.exe"
    Download $PyUrl $installer "Python $PyVersion (about 25 MB)"
    Info "Installing Python (just for you, no admin needed) ..."
    $p = Start-Process -FilePath $installer -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_test=0', 'Include_doc=0', 'Include_tcltk=1', 'Shortcuts=0')
    $p.WaitForExit()
    Remove-Item $installer -Force -ErrorAction SilentlyContinue
    $Python = Find-Python
    if (-not $Python) { Fail "Python could not be installed (installer exit code $($p.ExitCode))." }
    Ok "Installed: $Python"
}
$PythonW = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
if (-not (Test-Path $PythonW)) { $PythonW = $Python }

# ---------------------------------------------------------------- 2. Ollama
Step 2 "Ollama (the program that runs the AI model on your PC)"
$Ollama = Find-Ollama
if ($Ollama) {
    Ok "Found: $Ollama"
} else {
    $installer = Join-Path $env:TEMP 'OllamaSetup.exe'
    Download $OllamaUrl $installer "Ollama (about 1.9 GB, please wait)"
    Info "Installing Ollama ..."
    # Wait for the installer itself only. (Start-Process -Wait would also wait for the
    # Ollama app the installer opens at the end, so setup would hang until it is closed.)
    $p = Start-Process -FilePath $installer -PassThru -ArgumentList @('/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-')
    $p.WaitForExit()
    # Close the 'Welcome to Ollama' window it opens; AI Tutor runs Ollama hidden by itself.
    for ($i = 0; $i -lt 20; $i++) {
        $app = Get-Process -Name 'ollama app', 'Ollama' -ErrorAction SilentlyContinue
        if ($app) { Start-Sleep -Seconds 1; $app | Stop-Process -Force -ErrorAction SilentlyContinue; break }
        Start-Sleep -Milliseconds 500
    }
    Get-Process -Name 'ollama' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Remove-Item $installer -Force -ErrorAction SilentlyContinue
    $Ollama = Find-Ollama
    if (-not $Ollama) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($winget) {
            Info "Trying winget instead ..."
            & winget install --id Ollama.Ollama -e --silent --accept-source-agreements --accept-package-agreements | Out-Null
            $Ollama = Find-Ollama
        }
    }
    if (-not $Ollama) { Fail "Ollama could not be installed. Install it from https://ollama.com/download and run setup.bat again." }
    Ok "Installed: $Ollama"
}
$env:PATH = (Split-Path -Parent $Ollama) + ';' + $env:PATH

# ---------------------------------------------------------------- 3. Copy AI Tutor
Step 3 "Installing AI Tutor to $Dest"
Stop-Tutor
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
if ((Resolve-Path $Src).Path -ne (Resolve-Path $Dest).Path) {
    Remove-Item "$Dest\ai_tutor" -Recurse -Force -ErrorAction SilentlyContinue
    Copy-Item "$Src\ai_tutor" "$Dest\ai_tutor" -Recurse -Force
    Copy-Item "$Src\run.py", "$Src\README.md" $Dest -Force
    New-Item -ItemType Directory -Force -Path "$Dest\installer" | Out-Null
    Copy-Item "$Src\installer\*" "$Dest\installer" -Force
    # moving from an older copy: bring your settings, journal and question memory along
    foreach ($item in @('config.json', 'logs', 'memory')) {
        if ((Test-Path "$Src\$item") -and -not (Test-Path "$Dest\$item")) { Copy-Item "$Src\$item" "$Dest\$item" -Recurse -Force }
    }
}
Get-ChildItem $Dest -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $Dest -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
Ok "Done"

# ---------------------------------------------------------------- 4. AI model
Step 4 "Choosing and downloading the best AI model for this PC"
$env:PYTHONIOENCODING = 'utf-8'
Push-Location $Dest
& $Python "$Dest\run.py" --setup
$code = $LASTEXITCODE
Pop-Location
if ($code -ne 0) { Fail "The AI model could not be set up (see the message above). Run setup.bat again to retry." }

# ---------------------------------------------------------------- 5. Start with Windows
Step 5 "Making AI Tutor start automatically with Windows"
Remove-Item (Join-Path $Startup 'AI Tutor.bat') -Force -ErrorAction SilentlyContinue   # older versions
New-Shortcut (Join-Path $Startup 'AI Tutor.lnk') $PythonW "`"$Dest\run.py`"" $Dest 'AI Tutor (runs in the background)'
$menu = Join-Path $Programs 'AI Tutor'
New-Item -ItemType Directory -Force -Path $menu | Out-Null
New-Shortcut (Join-Path $menu 'Start AI Tutor.lnk') $PythonW "`"$Dest\run.py`"" $Dest 'Start AI Tutor if you closed it'
New-Shortcut (Join-Path $menu 'Uninstall AI Tutor.lnk') "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    "-NoProfile -ExecutionPolicy Bypass -File `"$Dest\installer\uninstall.ps1`"" $Dest 'Remove AI Tutor from this PC'
New-Shortcut (Join-Path $menu 'AI Tutor guide.lnk') "$Dest\README.md" '' $Dest 'How to use AI Tutor'
Ok "Done"

# ---------------------------------------------------------------- 6. Start it
Step 6 "Starting AI Tutor"
Start-Process -FilePath $PythonW -ArgumentList "`"$Dest\run.py`"" -WorkingDirectory $Dest
Start-Sleep -Seconds 2
Ok "AI Tutor is running in the background."

Write-Host ""
Write-Host "  ==========================================" -ForegroundColor Green
Write-Host "    All done! You never need to run setup again." -ForegroundColor Green
Write-Host "  ==========================================" -ForegroundColor Green
Write-Host ""
Write-Host "  How to use it (in VS Code, Notepad, Notepad++ ...):"
Write-Host "    Ctrl + S + T    AI reads your file, solves the question / finishes your code"
Write-Host "    `` (key left of 1)   pause, so you can code yourself / press again to continue"
Write-Host "    Numpad + / -    type faster / slower"
Write-Host "    Ctrl + S + Q    stop what the AI is doing"
Write-Host "    Ctrl + E + N    close the AI (Ctrl + S + T brings it back)"
Write-Host ""
Write-Host "  Uninstall any time: Start menu > AI Tutor > Uninstall AI Tutor"
Write-Host "  You can now close this window and delete the downloaded folder."
Write-Host ""
exit 0
