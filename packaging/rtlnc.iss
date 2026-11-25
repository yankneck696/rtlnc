; Inno Setup 6 script: builds dist\installer\rtlnc-setup-<version>.exe
#define AppVersion "0.3.0"

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

[Files]
Source: "..\dist\rtlnc\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{group}\rtlnc noise canceller"; Filename: "{app}\rtlnc.exe"
Name: "{autodesktop}\rtlnc"; Filename: "{app}\rtlnc.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked

[Run]
Filename: "{app}\rtlnc.exe"; Description: "Start rtlnc now"; Flags: nowait postinstall skipifsilent

[Messages]
FinishedLabel=Setup has finished.%n%nFirst time with RTL-SDR on this PC? Each dongle needs the WinUSB driver once: run Zadig (zadig.akeo.ie), Options > List All Devices, pick "Bulk-In, Interface (Interface 0)", choose WinUSB, Install Driver.
