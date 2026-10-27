; Inno Setup 6: установщик для Windows. Файл в UTF-8 с BOM — иначе кириллица сломается.
; Сборка: ISCC.exe /DAppVersion=1.0.0 packaging\installer.iss  (после pyinstaller)
#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif

[Setup]
AppId={{8E0B6C2A-5D1F-4C3B-9A77-2F4E1D9C6B10}
AppName=Разбор скриншотов
AppVersion={#AppVersion}
AppVerName=Разбор скриншотов {#AppVersion}
AppPublisher=Ismoil
DefaultDirName={localappdata}\Programs\Sortshots
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=Sortshots-Setup-{#AppVersion}
SetupIconFile=icon.ico
UninstallDisplayIcon={app}\Sortshots.exe
UninstallDisplayName=Разбор скриншотов
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\Sortshots\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Разбор скриншотов"; Filename: "{app}\Sortshots.exe"
Name: "{autodesktop}\Разбор скриншотов"; Filename: "{app}\Sortshots.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Sortshots.exe"; Description: "{cm:LaunchProgram,Разбор скриншотов}"; Flags: nowait postinstall skipifsilent
