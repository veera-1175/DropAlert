# Start Chrome for Epic login (profile on local C: drive)
$profile = "$env:LOCALAPPDATA\DropAlert\chrome-epic"
$chrome = "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) {
    $chrome = "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
}
New-Item -ItemType Directory -Force -Path $profile | Out-Null
Write-Host "Profile: $profile"
Write-Host "Close ALL other Chrome windows first!"
Start-Process $chrome @(
    "--remote-debugging-port=9222",
    "--user-data-dir=$profile",
    "--no-first-run",
    "https://store.epicgames.com/en-US/"
)
Write-Host "Then run: python hunter.py --login-only --stores epic"
