; Inno Setup script for FrameSnap. Built by packaging\build_windows.ps1:
;   ISCC.exe /DAppVersion=1.1.0 packaging\windows\installer.iss
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{8C4D1B1E-3F6A-4C58-9E0B-5A2F7D9E41B3}
AppName=FrameSnap
AppVersion={#AppVersion}
AppPublisher=le-wib
DefaultDirName={autopf}\FrameSnap
DefaultGroupName=FrameSnap
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\..\dist
OutputBaseFilename=FrameSnap-{#AppVersion}-Windows-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\icon.ico
UninstallDisplayIcon={app}\FrameSnap.exe
LicenseFile=..\..\LICENSE
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "..\..\dist\FrameSnap\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\FrameSnap"; Filename: "{app}\FrameSnap.exe"
Name: "{autodesktop}\FrameSnap"; Filename: "{app}\FrameSnap.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\FrameSnap.exe"; Description: "{cm:LaunchProgram,FrameSnap}"; Flags: nowait postinstall skipifsilent
