; Inno Setup script for KeepDesktop
; Build with:  ISCC.exe installer.iss
; Requires:    Inno Setup 6 (https://jrsoftware.org/isdl.php)
;
; The version is read from MyAppVersion below — keep it in sync with
; config.py's APP_VERSION (or pass /DMyAppVersion=x.y.z on the command line).

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

#define MyAppName        "KeepDesktop"
#define MyAppPublisher   "LukeCGG"
#define MyAppURL         "https://github.com/LukeCGG/Keep-Desktop"
#define MyAppExeName     "KeepDesktop.exe"

[Setup]
; Stable AppId — DO NOT change between versions, or upgrades become side-by-side installs
AppId={{8B1A7C4E-3F2D-4A91-9B2E-2A4D5E6F7A8B}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=Output
OutputBaseFilename=KeepDesktop-Setup-{#MyAppVersion}
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
; Allow updates to overwrite the running app
CloseApplications=yes
RestartApplications=yes
; Don't force a reboot if files are in use; we'll handle it
CloseApplicationsFilter=*.exe,*.dll,*.pyd

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked
Name: "startup";     Description: "Start {#MyAppName} when Windows starts"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; PyInstaller's --onedir output goes to dist\KeepDesktop\
Source: "dist\KeepDesktop\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "LICENSE";            DestDir: "{app}"; Flags: ignoreversion
Source: "README.md";          DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}";              Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}";    Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";        Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}";        Filename: "{app}\{#MyAppExeName}"; Tasks: startup

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Leave user data (%APPDATA%\KeepDesktop) intact on uninstall — users can wipe it manually
Type: filesandordirs; Name: "{app}"
