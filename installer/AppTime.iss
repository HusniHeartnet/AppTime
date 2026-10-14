; Inno Setup script for the Windows installer (AppTime-Setup.exe).
; Built by GitHub Actions:  ISCC /DMyVersion=1.7.0 installer\AppTime.iss
; Needs dist\AppTime.exe (the PyInstaller build) to exist first.
;
; Installs for the current user only (no admin password), into
; %LOCALAPPDATA%\Programs\AppTime, like VS Code, Discord or Spotify.

#ifndef MyVersion
  #define MyVersion "0.0.0"
#endif

[Setup]
; Never change AppId: Windows uses it to recognise AppTime for upgrades and uninstalling.
; platform_win.py reads this same id to keep the version in Settings > Apps current.
AppId={{6F3A2C4E-9B1D-4E7A-A5C2-3D8E1F0B7A94}
AppName=AppTime
AppVersion={#MyVersion}
AppVerName=AppTime {#MyVersion}
AppPublisher=Husni Heartnet
AppPublisherURL=https://github.com/HusniHeartnet/AppTime
AppSupportURL=https://github.com/HusniHeartnet/AppTime
AppUpdatesURL=https://github.com/HusniHeartnet/AppTime/releases/latest
VersionInfoVersion={#MyVersion}
VersionInfoDescription=AppTime installer
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\AppTime
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
UsePreviousAppDir=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\assets\AppTime.ico
UninstallDisplayIcon={app}\AppTime.exe
UninstallDisplayName=AppTime
WizardStyle=modern
WizardSizePercent=100
OutputDir=..\dist
OutputBaseFilename=AppTime-Setup
Compression=lzma2/max
SolidCompression=yes
; AppTime is closed by the [Code] below, so the installer doesn't ask about it.
CloseApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\AppTime.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\AppTime"; Filename: "{app}\AppTime.exe"; Comment: "See how long each app is on your screen"
Name: "{autodesktop}\AppTime"; Filename: "{app}\AppTime.exe"; Tasks: desktopicon

[Run]
; AppTime sets up starting at sign-in by itself when it runs.
Filename: "{app}\AppTime.exe"; Description: "Start AppTime"; Flags: nowait postinstall skipifsilent
; In-app updates run this installer silently with /APPTIMELAUNCH: start AppTime again afterwards.
Filename: "{app}\AppTime.exe"; Parameters: "--after-update"; Flags: nowait; Check: LaunchAfterUpdate

[UninstallDelete]
; Left behind by in-app updates of older versions.
Type: files; Name: "{app}\AppTime.old.exe"
Type: files; Name: "{app}\.AppTime.exe.download"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  StartupApprovedKey = 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run';

{ Close a running AppTime: first ask it to save and quit, then make sure. }
procedure QuitAppTime();
var
  Code: Integer;
begin
  Exec(ExpandConstant('{sys}\curl.exe'), '-s -m 5 -X POST http://127.0.0.1:47820/api/quit', '',
       SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(1500);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM AppTime.exe', '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(500);
end;

function LaunchAfterUpdate(): Boolean;
var
  I: Integer;
begin
  Result := False;
  for I := 1 to ParamCount do
    if CompareText(ParamStr(I), '/APPTIMELAUNCH') = 0 then
      Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  QuitAppTime();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  QuitAppTime();
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RegDeleteValue(HKEY_CURRENT_USER, RunKey, 'AppTime');
    RegDeleteValue(HKEY_CURRENT_USER, StartupApprovedKey, 'AppTime');
    if SuppressibleMsgBox('Also delete your tracked screen time history?' + #13#10 + #13#10 +
         'Choose No to keep it, so it''s still there if you install AppTime again.',
         mbConfirmation, MB_YESNO or MB_DEFBUTTON2, IDNO) = IDYES then
      DelTree(ExpandConstant('{localappdata}\AppTime'), True, True, True);
  end;
end;
