; Inno Setup script — wraps dist\Lorekeeper into a per-user installer (no admin rights needed).
; Built by CI:  iscc /DAppVersion=1.0.0 packaging\installer.iss

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6E0D3C64-1B5B-4C6F-9C8B-4C0B2B7A1F2E}
AppName=Lorekeeper
AppVersion={#AppVersion}
AppPublisher=Stoned Sheamus
DefaultDirName={localappdata}\Programs\Lorekeeper
DefaultGroupName=Lorekeeper
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Lorekeeper-Setup-{#AppVersion}
SetupIconFile=lorekeeper.ico
UninstallDisplayIcon={app}\Lorekeeper.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\Lorekeeper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Lorekeeper"; Filename: "{app}\Lorekeeper.exe"
Name: "{autodesktop}\Lorekeeper"; Filename: "{app}\Lorekeeper.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Lorekeeper.exe"; Description: "Launch Lorekeeper"; Flags: nowait postinstall skipifsilent

; Your library, settings and Whisper models live in %LOCALAPPDATA%\Lorekeeper and are kept on uninstall.
