# AI Tutor - uninstall (Start menu > AI Tutor > Uninstall AI Tutor)
$ErrorActionPreference = 'SilentlyContinue'
$Dest    = Join-Path $env:LOCALAPPDATA 'AI-Tutor'
$Startup = [Environment]::GetFolderPath('Startup')
$Menu    = Join-Path ([Environment]::GetFolderPath('Programs')) 'AI Tutor'

Write-Host ""
Write-Host "  AI Tutor - uninstall" -ForegroundColor Cyan
Write-Host ""
$answer = Read-Host "  Remove AI Tutor from this PC? (Y/N)"
if ($answer -notmatch '^[Yy]') { Write-Host "  Nothing was changed."; Start-Sleep 2; exit 0 }

# stop it
if (Test-Path "$Dest\logs\tutor.pid") {
    $id = Get-Content "$Dest\logs\tutor.pid" | Select-Object -First 1
    if ($id) { Stop-Process -Id ([int]$id) -Force }
}
Remove-Item (Join-Path $Startup 'AI Tutor.lnk') -Force
Remove-Item (Join-Path $Startup 'AI Tutor.bat') -Force
Write-Host "  - AI Tutor stopped and removed from Windows start-up"

# the AI model (several GB)
$ollama = Get-Command ollama -ErrorAction SilentlyContinue
$ollamaExe = if ($ollama) { $ollama.Source } elseif (Test-Path "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe") { "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" }
if ($ollamaExe) {
    $models = & $ollamaExe list 2>$null | Select-String -Pattern '^(qwen2\.5-coder:\S+)' | ForEach-Object { $_.Matches[0].Groups[1].Value }
    if ($models) {
        $answer = Read-Host "  Also delete the AI model to free disk space ($($models -join ', '))? (Y/N)"
        if ($answer -match '^[Yy]') {
            foreach ($m in $models) { & $ollamaExe rm $m | Out-Null }
            Write-Host "  - AI model deleted"
        }
    }
    Write-Host "  (Ollama itself stays installed; remove it in Settings > Apps if you don't need it.)"
}

$answer = Read-Host "  Also delete your mistakes journal and settings? (Y/N)"
$keep = $answer -notmatch '^[Yy]'
if ($keep -and (Test-Path "$Dest\logs\mistakes_journal.md")) {
    $docs = [Environment]::GetFolderPath('MyDocuments')
    Copy-Item "$Dest\logs\mistakes_journal.md" (Join-Path $docs 'AI Tutor mistakes journal.md') -Force
    Write-Host "  - Your mistakes journal was saved to Documents\AI Tutor mistakes journal.md"
}

Remove-Item $Menu -Recurse -Force
# delete the program folder after this window closes (this script lives in it)
Start-Process -WindowStyle Hidden -FilePath "$env:SystemRoot\System32\cmd.exe" -ArgumentList "/c timeout /t 3 /nobreak >nul & rmdir /s /q `"$Dest`""
Write-Host ""
Write-Host "  AI Tutor has been removed. Goodbye!" -ForegroundColor Green
Start-Sleep 3
