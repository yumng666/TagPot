; Tag Pot 安装包脚本（NSIS 3 / Unicode）
; 用 build_installer.bat 调用，产物在 dist\TagPot-Setup-<版本>.exe
;
; 采用「每用户安装」：装到 %LOCALAPPDATA%\Programs\TagPot，全程不需要管理员权限，
; 程序数据写 %LOCALAPPDATA%\TagPot（卸载时不动它）。

Unicode true

!define APPNAME     "Tag Pot"
!define APPEXE      "TagPot.exe"
!define APPVERSION  "1.0.0"
!define PUBLISHER   "yumng666"
!define UNINSTKEY   "Software\Microsoft\Windows\CurrentVersion\Uninstall\TagPot"

Name "${APPNAME}"
BrandingText "${APPNAME} ${APPVERSION}"
OutFile "..\dist\TagPot-Setup-${APPVERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\TagPot"
InstallDirRegKey HKCU "Software\TagPot" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 32

VIProductVersion "${APPVERSION}.0"
VIAddVersionKey /LANG=2052 "ProductName"     "${APPNAME}"
VIAddVersionKey /LANG=2052 "CompanyName"     "${PUBLISHER}"
VIAddVersionKey /LANG=2052 "FileDescription" "Tag Pot 安装程序"
VIAddVersionKey /LANG=2052 "FileVersion"     "${APPVERSION}"
VIAddVersionKey /LANG=2052 "ProductVersion"  "${APPVERSION}"
VIAddVersionKey /LANG=2052 "LegalCopyright"  "Copyright (C) 2026 yumng666"

!include "MUI2.nsh"

!define MUI_ICON   "..\assets\tagpot.ico"
!define MUI_UNICON "..\assets\tagpot.ico"
!define MUI_WELCOMEFINISHPAGE_BITMAP "welcome.bmp"
!define MUI_HEADERIMAGE
!define MUI_HEADERIMAGE_BITMAP "header.bmp"
!define MUI_HEADERIMAGE_RIGHT
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APPEXE}"
!define MUI_FINISHPAGE_RUN_TEXT "立即启动 Tag Pot"
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_COMPONENTS
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"

LangString DESC_Main    ${LANG_SIMPCHINESE} "Tag Pot 主程序（必需）。"
LangString DESC_Main    ${LANG_ENGLISH}     "Tag Pot application (required)."
LangString DESC_Desktop ${LANG_SIMPCHINESE} "在桌面创建快捷方式。"
LangString DESC_Desktop ${LANG_ENGLISH}     "Create a desktop shortcut."

Section "!Tag Pot" SecMain
  SectionIn RO
  SetOutPath "$INSTDIR"
  SetOverwrite try
  File "..\dist\TagPot.exe"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\Tag Pot"
  CreateShortcut "$SMPROGRAMS\Tag Pot\Tag Pot.lnk" "$INSTDIR\${APPEXE}" "" "$INSTDIR\${APPEXE}" 0
  CreateShortcut "$SMPROGRAMS\Tag Pot\卸载 Tag Pot.lnk" "$INSTDIR\Uninstall.exe"

  WriteRegStr HKCU "Software\TagPot" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName"     "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion"  "${APPVERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher"       "${PUBLISHER}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon"     "$INSTDIR\${APPEXE}"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINSTKEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
SectionEnd

Section "桌面快捷方式" SecDesktop
  CreateShortcut "$DESKTOP\Tag Pot.lnk" "$INSTDIR\${APPEXE}" "" "$INSTDIR\${APPEXE}" 0
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain}    $(DESC_Main)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} $(DESC_Desktop)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  Delete "$DESKTOP\Tag Pot.lnk"
  Delete "$SMPROGRAMS\Tag Pot\Tag Pot.lnk"
  Delete "$SMPROGRAMS\Tag Pot\卸载 Tag Pot.lnk"
  RMDir  "$SMPROGRAMS\Tag Pot"

  Delete "$INSTDIR\${APPEXE}"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir  "$INSTDIR"

  DeleteRegKey HKCU "${UNINSTKEY}"
  DeleteRegKey HKCU "Software\TagPot"
SectionEnd
