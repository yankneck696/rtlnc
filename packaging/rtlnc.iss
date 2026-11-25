; Inno Setup 6 script: builds dist\installer\rtlnc-setup-<version>.exe
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

[Setup]
AppId={{5C2F3E3A-8E7B-4C1D-9A51-0D2C6E8B7F41}
AppName=rtlnc noise canceller
AppVersion={#AppVersion}
AppPublisher=rtlnc
DefaultDirName={autopf}\rtlnc
DefaultGroupName=rtlnc
OutputDir=..\dist\installer
OutputBaseFilename=rtlnc-setup-{#AppVersion}
SetupIconFile=rtlnc.ico
UninstallDisplayIcon={app}\rtlnc.exe
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequiredOverridesAllowed=dialog
; rtlnc's helper processes have no windows, so "please close the app" can't
; work: Setup ends any running rtlnc.exe itself (see [Code]).
CloseApplications=force
RestartApplications=no

[Files]
Source: "..\dist\rtlnc\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\rtlnc noise canceller"; Filename: "{app}\rtlnc.exe"
Name: "{autodesktop}\rtlnc"; Filename: "{app}\rtlnc.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\rtlnc.exe"; Description: "Start rtlnc now"; Flags: nowait postinstall skipifsilent

[Code]
procedure EndRunningRtlnc();
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /T /IM rtlnc.exe', '', SW_HIDE,
       ewWaitUntilTerminated, ResultCode);
  Sleep(500);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  EndRunningRtlnc();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  EndRunningRtlnc();
  Result := True;
end;

[Messages]
FinishedLabel=Setup has finished.%n%nFirst time with RTL-SDR on this PC? Each dongle needs the WinUSB driver once: run Zadig (zadig.akeo.ie), Options > List All Devices, pick "Bulk-In, Interface (Interface 0)", choose WinUSB, Install Driver.
