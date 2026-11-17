; Artemis Windows kurucu betiği (Inno Setup 6).
; Derleme betiği /DMyAppVersion=... parametresiyle sürümü geçer.
#ifndef MyAppVersion
  #define MyAppVersion "0.2.0"
#endif

[Setup]
AppId={{8D6D4A7E-4D1B-4C9A-9D5F-3B2E7A1C6F04}
AppName=Artemis
AppVersion={#MyAppVersion}
AppPublisher=Artemis
DefaultDirName={localappdata}\Programs\Artemis
DefaultGroupName=Artemis
PrivilegesRequired=lowest
DisableProgramGroupPage=yes
SetupIconFile=..\artemis\channels\desktop\artemis.ico
UninstallDisplayIcon={app}\Artemis.exe
OutputDir=..\dist\installer
OutputBaseFilename=Artemis-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "turkish"; MessagesFile: "compiler:Languages\Turkish.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\Artemis\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Artemis"; Filename: "{app}\Artemis.exe"
Name: "{autodesktop}\Artemis"; Filename: "{app}\Artemis.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Artemis.exe"; Description: "{cm:LaunchProgram,Artemis}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Yalnızca kurulum klasörü temizlenir; kullanıcı verileri (%APPDATA%\Artemis) korunur.
Type: filesandordirs; Name: "{app}"
