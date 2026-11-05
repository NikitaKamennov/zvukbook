#ifndef BundleDir
  #error BundleDir is required
#endif
#ifndef ReleaseDir
  #error ReleaseDir is required
#endif
#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif
#ifndef Edition
  #define Edition "full"
#endif
#if Edition == "cpu"
  #define ProductName "Звуккниг CPU"
  #define ProductDirectory "Zvukbook-CPU"
  #define ProductId "{{B4162A76-871D-4D82-9D95-E66F17E6C9B0}"
  #define SetupBaseName "Zvukbook-CPU-Setup"
#elif Edition == "full"
  #define ProductName "Звуккниг"
  #define ProductDirectory "Zvukbook"
  #define ProductId "{{7B98E39A-89DF-46BF-8D78-5EA514F6356B}"
  #define SetupBaseName "Zvukbook-Setup"
#else
  #error Unknown edition
#endif

[Setup]
AppId={#ProductId}
AppName={#ProductName}
AppVersion={#AppVersion}
AppPublisher=Zvukbook
DefaultDirName={localappdata}\Programs\{#ProductDirectory}
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.19041
OutputDir={#ReleaseDir}
OutputBaseFilename={#SetupBaseName}
Compression=zip/6
SolidCompression=no
WizardStyle=modern
UninstallDisplayIcon={app}\Zvukbook.exe
AppMutex=Local\Zvukbook.Desktop
CloseApplications=no
RestartApplications=no
SetupLogging=yes
ExtraDiskSpaceRequired=5368709120
InfoBeforeFile={#BundleDir}\START-HERE.txt

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\{#ProductName}"; Filename: "{app}\Zvukbook.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\{#ProductName}"; Filename: "{app}\Zvukbook.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Zvukbook.exe"; Description: "Запустить Звуккниг"; Flags: nowait postinstall skipifsilent

[Code]
function CreateFileW(FileName: string; DesiredAccess, ShareMode, SecurityAttributes, CreationDisposition, FlagsAndAttributes, TemplateFile: LongWord): THandle;
  external 'CreateFileW@kernel32.dll stdcall';
function CloseHandle(Handle: THandle): Boolean;
  external 'CloseHandle@kernel32.dll stdcall';

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  LockPath: String;
  Handle: THandle;
begin
  Result := '';
  LockPath := ExpandConstant('{localappdata}\Zvukbook\desktop.lock');
  if FileExists(LockPath) then begin
    Handle := CreateFileW(LockPath, $80000000, 0, 0, 3, 0, 0);
    if Handle = THandle(-1) then
      Result := 'Закрой Звуккниг и дождись завершения обработчиков перед установкой. Книги будут сохранены.'
    else
      CloseHandle(Handle);
  end;
end;

function InitializeUninstall(): Boolean;
var
  LockPath: String;
  Handle: THandle;
begin
  Result := True;
  LockPath := ExpandConstant('{localappdata}\Zvukbook\desktop.lock');
  if FileExists(LockPath) then begin
    Handle := CreateFileW(LockPath, $80000000, 0, 0, 3, 0, 0);
    if Handle = THandle(-1) then begin
      MsgBox('Сначала закрой Звуккниг и дождись остановки обработки.', mbInformation, MB_OK);
      Result := False;
    end else
      CloseHandle(Handle);
  end;
end;
