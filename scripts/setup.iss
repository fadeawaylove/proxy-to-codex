; ── proxy-to-codex Inno Setup Installer ──────────────────────
; Build with: iscc /DMyAppVersion=0.1.9 scripts\setup.iss

#define MyAppName "proxy-to-codex"
#define MyAppPublisher "fadeawaylove"
#define MyAppURL "https://github.com/fadeawaylove/proxy-to-codex"
#define MyAppExeName "proxy-to-codex.exe"

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{F4D8C9E2-5A7B-4C1F-9E3D-0B6A8F2C1D4E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=proxy-to-codex_setup_v{#MyAppVersion}
SetupIconFile=..\icon.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\icon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkablealone
Name: "autostart"; Description: "开机自启"; GroupDescription: "其他任务:"

[Files]
Source: "..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\icon.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"
Name: "{autoprograms}\{#MyAppName}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\icon.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: "{app}\{#MyAppExeName}"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Wnd: LongWord;
  Timeout: Integer;
begin
  Result := '';

  { Find the proxy-to-codex window by title }
  Wnd := FindWindowByWindowName('Codex 代理管理');
  if Wnd = 0 then
    Exit;

  { Send WM_CLOSE; _on_close will stop server and call os._exit(0) }
  PostMessage(Wnd, 16 { WM_CLOSE }, 0, 0);

  { Wait up to 5 seconds for the window to close }
  Timeout := 0;
  while (Timeout < 50) and (FindWindowByWindowName('Codex 代理管理') <> 0) do
  begin
    Sleep(100);
    Timeout := Timeout + 1;
  end;
end;
