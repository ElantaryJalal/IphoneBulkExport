; Inno Setup script for iPhone Bulk Exporter.
;
; Produces dist\iPhoneExporterSetup.exe — a single installer that:
;   1. installs iPhoneExporter.exe (ffmpeg is bundled inside it),
;   2. silently installs the Apple Mobile Device USB driver via winget so the
;      fast AFC path works on first launch — no separate iTunes / Apple Devices
;      install needed. If that can't run, the app still works over MTP.
;
; Build (after build_venv.bat has produced dist\iPhoneExporter.exe):
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss
; Compile from the project root so the relative paths below resolve.

#define MyAppName "iPhone Bulk Exporter"
#define MyAppVersion "1.0"
#define MyAppPublisher "Jalal Elantary"
#define MyAppExe "iPhoneExporter.exe"
#define MyAppUrl "https://iphone-bulk-export.vercel.app/"

[Setup]
AppId={{8E5B2C41-7F3A-4E2D-9C1B-IPHONEEXPORT01}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppUrl}
DefaultDirName={autopf}\iPhoneExporter
DefaultGroupName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExe}
OutputDir=dist
OutputBaseFilename=iPhoneExporterSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Driver install needs admin; UAC elevates in-place for the same user.
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional icons:"

[Files]
Source: "dist\{#MyAppExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "installer\ensure_apple_driver.ps1"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
; One-time: set up the iPhone USB driver so AFC works immediately.
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\ensure_apple_driver.ps1"""; \
  StatusMsg: "Setting up the iPhone USB driver (one-time, needs internet)…"; \
  Flags: runhidden waituntilterminated
; Offer to launch the app when done.
Filename: "{app}\{#MyAppExe}"; Description: "Launch {#MyAppName} now"; \
  Flags: nowait postinstall skipifsilent
