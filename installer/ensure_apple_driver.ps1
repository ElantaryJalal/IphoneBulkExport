# ensure_apple_driver.ps1 — run by the installer (elevated) to make the AFC
# path work out of the box.
#
# It installs the Apple Mobile Device USB driver + usbmuxd service (Apple's
# official "Apple Mobile Device Support" package) via winget — NOT iTunes, NOT
# the full Apple Devices app. If the driver is already present, or winget isn't
# available, it does nothing and exits 0: the app still works over MTP on
# machines with no Apple software, so this step must never block the install.

$ErrorActionPreference = 'SilentlyContinue'

function Test-AppleDriverPresent {
    if (Get-Service 'Apple Mobile Device Service') { return $true }
    if (Test-Path "$env:CommonProgramFiles\Apple\Mobile Device Support") { return $true }
    if (Get-AppxPackage -Name 'AppleInc.AppleDevices') { return $true }
    try {
        if ((Test-NetConnection 127.0.0.1 -Port 27015 -WarningAction SilentlyContinue).TcpTestSucceeded) {
            return $true
        }
    } catch { }
    return $false
}

if (Test-AppleDriverPresent) {
    Write-Output 'Apple Mobile Device driver already present - nothing to do.'
    exit 0
}

# Locate winget. The per-user alias resolves under same-user UAC elevation;
# fall back to the explicit alias path for the cross-user case.
$winget = (Get-Command winget.exe -ErrorAction SilentlyContinue).Source
if (-not $winget) {
    $alias = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\winget.exe'
    if (Test-Path $alias) { $winget = $alias }
}

if (-not $winget) {
    Write-Output 'winget not found - skipping driver install. The app will use MTP.'
    exit 0
}

Write-Output "Installing Apple Mobile Device Support via $winget ..."
& $winget install --id Apple.AppleMobileDeviceSupport --source winget `
    --silent --accept-package-agreements --accept-source-agreements `
    --disable-interactivity
Write-Output "winget finished (exit $LASTEXITCODE)."

# Always succeed: a failed driver install must not fail the app install.
exit 0
