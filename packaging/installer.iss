; Inno Setup 6 script for Sims 4 Vertical Video Studio
#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

[Setup]
AppId={{8F6C3E1A-5B4D-4D2E-9F1A-7C8B9E0D1A2B}
AppName=Sims 4 Vertical Video Studio
AppVersion={#AppVersion}
AppPublisher=Sims Vertical Studio
DefaultDirName={localappdata}\Programs\SimsVerticalStudio
DefaultGroupName=Sims 4 Vertical Video Studio
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\release-out
OutputBaseFilename=SimsVerticalStudio-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\SimsVerticalStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Sims 4 Vertical Video Studio"; Filename: "{app}\SimsVerticalStudio.exe"
Name: "{autodesktop}\Sims 4 Vertical Video Studio"; Filename: "{app}\SimsVerticalStudio.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\SimsVerticalStudio.exe"; Description: "{cm:LaunchProgram,Sims 4 Vertical Video Studio}"; Flags: nowait postinstall
