; Instalador de Fire Station (Inno Setup 6). Se compila con tools/build_installer.py,
; que pasa la versión de app/__init__.py:  ISCC /DMyAppVersion=1.0.0 tools\installer.iss
;
; Dónde se instala: {localappdata}\Programs\FireStation, por usuario y SIN permisos
; de administrador. No va a Program Files a propósito: la app escribe sus datos al
; lado del .exe (data\, output\, logs\, resources\ -- ver app/paths.get_writable_dir)
; y el auto-updater reemplaza _internal\ en esa misma carpeta; en Program Files un
; usuario común no puede escribir y la app fallaría al guardar un parte.
;
; Datos del cuartel (CRÍTICO): el instalador NO trae ni toca data\, output\, logs\
; ni resources\ de al lado del .exe. Esas carpetas las crea la app en su primer
; arranque; reinstalar, actualizar o desinstalar las deja como están.

#ifndef MyAppVersion
  #define MyAppVersion "1.0.5"
#endif
#define MyAppName "Fire Station - Cuartel Adelia María"
#define MyAppShortName "Fire Station"
#define MyAppPublisher "Bomberos Voluntarios Adelia María"
#define MyAppExeName "FireStation.exe"
#define DistDir "..\dist\FireStation"

[Setup]
; AppId fijo: identifica la app entre versiones (reinstalar = actualizar). NO cambiarlo.
AppId={{6B0E7C1F-3F5D-4A8E-9C27-5E1A2D9B4F61}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
VersionInfoCompany={#MyAppPublisher}
VersionInfoProductName={#MyAppShortName}
DefaultDirName={localappdata}\Programs\FireStation
DefaultGroupName={#MyAppShortName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
UsePreviousAppDir=yes
OutputDir=..\dist
OutputBaseFilename=FireStation_Setup
SetupIconFile=..\resources\app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; Si Fire Station está abierta, se ofrece cerrarla antes de copiar (y reabrirla al final).
CloseApplications=yes
RestartApplications=yes

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Tasks]
Name: "desktopicon"; Description: "Crear un acceso directo en el Escritorio"; GroupDescription: "Accesos directos:"

[Files]
; Solo el programa: el .exe y _internal\. Las carpetas de datos se excluyen aunque
; alguna vez aparezcan dentro de dist\ (p. ej. si se abrió el .exe desde ahí).
; _internal\ms-playwright\ (Chromium para RUBA) viaja acá adentro: la app apunta
; PLAYWRIGHT_BROWSERS_PATH a esa carpeta al arrancar (app/paths.py). build_exe.py
; embebe EXACTAMENTE la revisión que pide la Playwright empaquetada y
; build_installer.py lo verifica antes de compilar. Si aun así faltara, la app
; usa Chrome/Edge de Windows o el Chromium de %LOCALAPPDATA%\ms-playwright.
Source: "{#DistDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#DistDir}\_internal\*"; DestDir: "{app}\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#MyAppShortName}\{#MyAppShortName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\{#MyAppShortName}\Desinstalar {#MyAppShortName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppShortName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Abrir {#MyAppShortName}"; Flags: nowait postinstall skipifsilent

[InstallDelete]
; Al reinstalar sobre una versión anterior: _internal\ se reemplaza entero (así no
; quedan librerías viejas mezcladas). NUNCA se nombran data\, output\, logs\ ni resources\.
Type: filesandordirs; Name: "{app}\_internal"

[UninstallDelete]
; El auto-updater pudo agregar archivos a _internal\ que el desinstalador no registró.
; Se borra solo el programa: los datos del cuartel quedan en la carpeta.
Type: filesandordirs; Name: "{app}\_internal"

[Code]
function InitializeUninstall(): Boolean;
begin
  Result := MsgBox('Se va a desinstalar Fire Station.' + #13#10 + #13#10 +
    'Los datos del cuartel (partes, base de datos, configuración, planillas y firmas) NO se borran: ' +
    'quedan en ' + ExpandConstant('{app}') + ' por si reinstalás.' + #13#10 + #13#10 +
    '¿Continuar?', mbConfirmation, MB_YESNO) = IDYES;
end;
