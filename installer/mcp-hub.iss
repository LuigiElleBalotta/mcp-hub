; Single installer for the Windows build: installs mcp-hub.exe (hub + CLI) and
; mcp-hub-gui.exe (the GUI) together, per user, with no administrator rights.
;
; The two executables stay separate on purpose and stay published as release
; assets with these exact names: the GUI's self-update downloads and replaces them
; by name, and the GUI starts the hub from the mcp-hub.exe sitting next to it. The
; GUI is a windowed program with no console, so merging it with the CLI would leave
; `mcp-hub apply` / `secrets` / `groups` with nowhere to print.
;
; Built by .github/workflows/release.yml:
;   ISCC /DVersion=<tag> /DDistDir=<dir with the two exes> installer\mcp-hub.iss
; The user's data (%LOCALAPPDATA%\mcp-hub: config, backups, cache) is never touched
; by install, update or uninstall.

#ifndef Version
  #define Version "0.0.0-dev"
#endif
#ifndef DistDir
  #define DistDir "..\dist"
#endif

[Setup]
AppId={{8C7C3D2B-5A0E-4B6B-9E1F-6D3B1A2C4F70}
AppName=mcp-hub
AppVersion={#Version}
AppVerName=mcp-hub {#Version}
AppPublisher=Luigi Balotta
AppPublisherURL=https://github.com/LuigiElleBalotta/mcp-hub
AppSupportURL=https://github.com/LuigiElleBalotta/mcp-hub/issues
DefaultDirName={localappdata}\Programs\mcp-hub
DefaultGroupName=mcp-hub
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#DistDir}
OutputBaseFilename=mcp-hub-setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\mcp-hub-gui.exe
UninstallDisplayName=mcp-hub
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Installing over a running copy (an update by installer): close the programs
; instead of failing on locked files.
CloseApplications=yes
CloseApplicationsFilter=mcp-hub-gui.exe,mcp-hub.exe
RestartApplications=no

[Languages]
Name: "italian"; MessagesFile: "compiler:Languages\Italian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Aggiungi mcp-hub al PATH (per usare il comando ""mcp-hub"" da terminale)"; Flags: unchecked
Name: "desktopicon"; Description: "Crea un'icona sul desktop"; Flags: unchecked

[Files]
Source: "{#DistDir}\mcp-hub.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#DistDir}\mcp-hub-gui.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\mcp-hub"; Filename: "{app}\mcp-hub-gui.exe"
Name: "{group}\Disinstalla mcp-hub"; Filename: "{uninstallexe}"
Name: "{autodesktop}\mcp-hub"; Filename: "{app}\mcp-hub-gui.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}'))

[Run]
Filename: "{app}\mcp-hub-gui.exe"; Description: "Avvia mcp-hub"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; The hub and its child servers must not outlive the files they run from. Only the
; processes started FROM THIS FOLDER are stopped (by path, not by name): another copy of
; mcp-hub elsewhere on the machine, e.g. a development one, is none of the uninstaller's business.
Filename: "powershell.exe"; Parameters: "-NoProfile -NonInteractive -Command ""Get-Process | Where-Object Path -like '{app}\*' | Stop-Process -Force"""; Flags: runhidden; RunOnceId: "StopMcpHub"

[Code]
function NeedsAddPath(Dir: string): Boolean;
var
  Paths: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then
    Paths := '';
  Result := Pos(';' + Lowercase(Dir) + ';', ';' + Lowercase(Paths) + ';') = 0;
end;

procedure RemoveFromPath(Dir: string);
var
  Paths, Rest, Item, Kept: string;
  P: Integer;
begin
  { Only touch the user's PATH if our folder is really in it. }
  if NeedsAddPath(Dir) then
    Exit;
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', Paths) then
    Exit;
  Rest := Paths;
  Kept := '';
  while Rest <> '' do
  begin
    P := Pos(';', Rest);
    if P = 0 then
    begin
      Item := Rest;
      Rest := '';
    end
    else
    begin
      Item := Copy(Rest, 1, P - 1);
      Delete(Rest, 1, P);
    end;
    if (Item <> '') and (CompareText(Item, Dir) <> 0) then
    begin
      if Kept <> '' then
        Kept := Kept + ';';
      Kept := Kept + Item;
    end;
  end;
  RegWriteExpandStringValue(HKCU, 'Environment', 'Path', Kept);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;
