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
; Kullanıcı başına kurulum {localappdata}\Programs\Artemis altına yapılır; klasör seçimine gerek yoktur.
; [UninstallDelete] {app} dizinini tamamen sildiği için Belgeler gibi seçilen bir klasör silinebilirdi.
DisableDirPage=yes
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
; Kurulan dosyaları Inno kendisi kaldırır; burada yalnız boş kalan klasör silinir. filesandordirs
; /DIR= ile seçilmiş bir klasörü (ör. Belgeler) tümüyle silebilirdi. Kullanıcı verisi %APPDATA%\Artemis.
Type: dirifempty; Name: "{app}"
