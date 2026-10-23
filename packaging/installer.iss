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
; The AI engine (llama.cpp), the search model and Whisper are inside this installer. The answer model is
; too big for one GitHub release file, so Lorekeeper downloads the one picked here the first time it opens.
Name: "aimodel"; Description: "Download an answer model the first time Lorekeeper opens"; GroupDescription: "AI model (runs on this PC, works offline once downloaded):"
Name: "aimodel\auto"; Description: "Pick the best fit for this PC automatically"; GroupDescription: "AI model (runs on this PC, works offline once downloaded):"; Flags: exclusive
Name: "aimodel\e4b"; Description: "Gemma 4 E4B: best answers (4.8 GB download, 12 GB+ RAM)"; GroupDescription: "AI model (runs on this PC, works offline once downloaded):"; Flags: exclusive unchecked
Name: "aimodel\e2b"; Description: "Gemma 4 E2B: lighter and faster (3.2 GB download)"; GroupDescription: "AI model (runs on this PC, works offline once downloaded):"; Flags: exclusive unchecked

[Dirs]
Name: "{localappdata}\Lorekeeper"; Flags: uninsneveruninstall

[INI]
Filename: "{localappdata}\Lorekeeper\install.ini"; Section: "model"; Key: "preset"; String: "auto"; Tasks: aimodel\auto
Filename: "{localappdata}\Lorekeeper\install.ini"; Section: "model"; Key: "preset"; String: "gemma-4-e4b"; Tasks: aimodel\e4b
Filename: "{localappdata}\Lorekeeper\install.ini"; Section: "model"; Key: "preset"; String: "gemma-4-e2b"; Tasks: aimodel\e2b
Filename: "{localappdata}\Lorekeeper\install.ini"; Section: "model"; Key: "preset"; String: "none"; Tasks: not aimodel

[Files]
Source: "..\dist\Lorekeeper\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Lorekeeper"; Filename: "{app}\Lorekeeper.exe"
Name: "{autodesktop}\Lorekeeper"; Filename: "{app}\Lorekeeper.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Lorekeeper.exe"; Description: "Launch Lorekeeper"; Flags: nowait postinstall skipifsilent

; Your library, settings and downloaded AI models live in %LOCALAPPDATA%\Lorekeeper and are kept on uninstall.
