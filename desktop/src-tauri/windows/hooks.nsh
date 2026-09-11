; PhotoAI installer safety hooks.
; The stock Tauri uninstaller owns the opt-in "delete app data" checkbox.  These
; hooks add product marker validation and a second, exact-path confirmation before
; that checkbox is allowed to remove the separately selected Content Root.

Var PhotoAIContentRoot
Var PhotoAIContentSizeKb
Var PhotoAIContentSizeMb
Var PhotoAIContentDeleteApproved
Var PhotoAIRollbackDir
Var PhotoAIRollbackActive
Var PhotoAIPreviousDisplayVersion
Var PhotoAIPreviousInstallLocation
Var PhotoAIPreviousDisplayName
Var PhotoAIInstallMarkerContents
Var PhotoAIFreshInstall
Var PhotoAISelfTestExit

!macro NSIS_HOOK_PREINSTALL
  ; Keep the previous program tree beside the install directory until the new
  ; tree has passed its own self-test.  CONTENT_ROOT is external and is never
  ; part of this swap.  Moving on the same volume is atomic and avoids making a
  ; second large copy on the user's small system disk.
  StrCpy $PhotoAIRollbackActive 0
  StrCpy $PhotoAIFreshInstall 0
  ReadRegStr $PhotoAIPreviousInstallLocation SHCTX "${UNINSTKEY}" "InstallLocation"
  ReadRegStr $PhotoAIPreviousDisplayName SHCTX "${UNINSTKEY}" "DisplayName"
  IfFileExists "$INSTDIR\${MAINBINARYNAME}.exe" photoai_preinstall_validate_update photoai_preinstall_validate_fresh

  photoai_preinstall_validate_update:
  ; An executable at the selected path is not sufficient proof of ownership.
  ; Refuse to rename it unless the complete previous-install identity matches.
  ; Tauri stores InstallLocation as a quoted command-path value. Match that
  ; exact representation; do not trim, canonicalize or accept a prefix.
  StrCmp $PhotoAIPreviousInstallLocation "$\"$INSTDIR$\"" 0 photoai_preinstall_not_owned
  StrCmp $PhotoAIPreviousDisplayName "${PRODUCTNAME}" 0 photoai_preinstall_not_owned
  IfFileExists "$INSTDIR\uninstall.exe" 0 photoai_preinstall_not_owned
  IfFileExists "$INSTDIR\.photoai-install-marker" 0 photoai_preinstall_not_owned
  ClearErrors
  FileOpen $0 "$INSTDIR\.photoai-install-marker" r
  ${If} ${Errors}
    Goto photoai_preinstall_not_owned
  ${EndIf}
  FileRead $0 $PhotoAIInstallMarkerContents 64
  FileClose $0
  StrCmp $PhotoAIInstallMarkerContents "PHOTO_AI_INSTALL/1" 0 photoai_preinstall_not_owned

  !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"
  StrCpy $PhotoAIRollbackDir "$INSTDIR.photoai-previous"
  IfFileExists "$PhotoAIRollbackDir\." 0 photoai_preinstall_backup
    MessageBox MB_OK|MB_ICONSTOP "发现尚未清理的旧版暂存目录：$\r$\n$PhotoAIRollbackDir$\r$\n$\r$\n为避免覆盖可恢复版本，安装已停止。"
    Abort

  photoai_preinstall_backup:
  ReadRegStr $PhotoAIPreviousDisplayVersion SHCTX "${UNINSTKEY}" "DisplayVersion"
  SetOutPath "$TEMP"
  ClearErrors
  Rename "$INSTDIR" "$PhotoAIRollbackDir"
  ${If} ${Errors}
    MessageBox MB_OK|MB_ICONSTOP "无法暂存当前版本，安装未修改。请关闭占用程序目录的应用后重试。"
    Abort
  ${EndIf}
  CreateDirectory "$INSTDIR"
  StrCpy $PhotoAIRollbackActive 1
  Goto photoai_preinstall_create_marker

  photoai_preinstall_not_owned:
  MessageBox MB_OK|MB_ICONSTOP "所选目录中的现有程序未通过照片选片安装所有权校验，安装未修改。请使用原安装位置更新，或选择一个空目录重新安装。"
  Abort

  photoai_preinstall_validate_fresh:
  ; A fresh install may use a missing or genuinely empty directory. It may also
  ; recover the exact two files that an older PhotoAI uninstaller could leave
  ; behind while deleting itself: our validated marker and uninstall.exe.
  ; Stale registration or any unknown directory entry still fails closed.
  StrCmp $PhotoAIPreviousInstallLocation "" 0 photoai_preinstall_not_owned
  StrCmp $PhotoAIPreviousDisplayName "" 0 photoai_preinstall_not_owned
  IfFileExists "$INSTDIR\.photoai-install-marker" photoai_preinstall_recover_stale 0
  StrCpy $PhotoAIFreshInstall 1
  CreateDirectory "$INSTDIR"
  Goto photoai_preinstall_create_marker

  photoai_preinstall_recover_stale:
  ClearErrors
  FileOpen $0 "$INSTDIR\.photoai-install-marker" r
  ${If} ${Errors}
    Goto photoai_preinstall_fresh_not_empty
  ${EndIf}
  FileRead $0 $PhotoAIInstallMarkerContents 64
  FileClose $0
  StrCmp $PhotoAIInstallMarkerContents "PHOTO_AI_INSTALL/1" 0 photoai_preinstall_fresh_not_empty

  ClearErrors
  FindFirst $0 $1 "$INSTDIR\*"
  ${If} ${Errors}
    Goto photoai_preinstall_fresh_not_empty
  ${EndIf}

  photoai_preinstall_recover_scan:
  StrCmp $1 "." photoai_preinstall_recover_next
  StrCmp $1 ".." photoai_preinstall_recover_next
  StrCmp $1 ".photoai-install-marker" photoai_preinstall_recover_next
  StrCmp $1 "uninstall.exe" photoai_preinstall_recover_next
  FindClose $0
  Goto photoai_preinstall_fresh_not_empty

  photoai_preinstall_recover_next:
  ClearErrors
  FindNext $0 $1
  ${If} ${Errors}
    FindClose $0
    Goto photoai_preinstall_recover_delete
  ${EndIf}
  Goto photoai_preinstall_recover_scan

  photoai_preinstall_recover_delete:
  SetOutPath "$TEMP"
  Delete "$INSTDIR\uninstall.exe"
  IfFileExists "$INSTDIR\uninstall.exe" photoai_preinstall_recover_busy 0
  Delete "$INSTDIR\.photoai-install-marker"
  RMDir "$INSTDIR"
  StrCpy $PhotoAIFreshInstall 1
  CreateDirectory "$INSTDIR"
  Goto photoai_preinstall_create_marker

  photoai_preinstall_recover_busy:
  MessageBox MB_OK|MB_ICONSTOP "旧卸载程序仍被占用，暂时无法清理。请等待卸载窗口完全关闭后重试；若仍存在，请重启 Windows 后再次安装。"
  Abort

  photoai_preinstall_fresh_not_empty:
  MessageBox MB_OK|MB_ICONSTOP "全新安装只允许使用空目录。所选目录包含其他文件，安装未修改。"
  Abort

  photoai_preinstall_create_marker:
  ; This marker is created before payload extraction. Future updates require its
  ; exact contents in addition to registry and executable ownership evidence.
  ClearErrors
  FileOpen $0 "$INSTDIR\.photoai-install-marker" w
  ${If} ${Errors}
    Goto photoai_preinstall_marker_failed
  ${EndIf}
  ClearErrors
  FileWrite $0 "PHOTO_AI_INSTALL/1"
  ${If} ${Errors}
    FileClose $0
    Goto photoai_preinstall_marker_failed
  ${EndIf}
  FileClose $0

  ${If} $PhotoAIFreshInstall == 1
    ClearErrors
    FindFirst $0 $1 "$INSTDIR\*"
    ${If} ${Errors}
      Goto photoai_preinstall_fresh_verify_failed
    ${EndIf}

    photoai_preinstall_fresh_scan:
    StrCmp $1 "." photoai_preinstall_fresh_next
    StrCmp $1 ".." photoai_preinstall_fresh_next
    StrCmp $1 ".photoai-install-marker" photoai_preinstall_fresh_next
    FindClose $0
    Goto photoai_preinstall_fresh_not_empty_after_marker

    photoai_preinstall_fresh_next:
    ClearErrors
    FindNext $0 $1
    ${If} ${Errors}
      FindClose $0
      Goto photoai_preinstall_marker_ready
    ${EndIf}
    Goto photoai_preinstall_fresh_scan

    photoai_preinstall_fresh_verify_failed:
    SetOutPath "$TEMP"
    Delete "$INSTDIR\.photoai-install-marker"
    RMDir "$INSTDIR"
    MessageBox MB_OK|MB_ICONSTOP "无法确认安装目录为空，安装未修改。请选择一个可写的空目录后重试。"
    Abort

    photoai_preinstall_fresh_not_empty_after_marker:
    SetOutPath "$TEMP"
    Delete "$INSTDIR\.photoai-install-marker"
    RMDir "$INSTDIR"
    MessageBox MB_OK|MB_ICONSTOP "全新安装只允许使用空目录。所选目录包含其他文件，安装未修改。"
    Abort
  ${EndIf}

  photoai_preinstall_marker_ready:
  SetOutPath "$INSTDIR"
  Goto photoai_preinstall_done

  photoai_preinstall_marker_failed:
  SetOutPath "$TEMP"
  Delete "$INSTDIR\.photoai-install-marker"
  RMDir "$INSTDIR"
  ${If} $PhotoAIRollbackActive == 1
    ClearErrors
    Rename "$PhotoAIRollbackDir" "$INSTDIR"
    ${If} ${Errors}
      MessageBox MB_OK|MB_ICONSTOP "无法创建安装所有权标记，且原版本未能自动恢复。原版本仍保留在：$\r$\n$PhotoAIRollbackDir$\r$\n$\r$\n请勿删除该目录。"
      Abort
    ${EndIf}
    StrCpy $PhotoAIRollbackActive 0
  ${EndIf}
  MessageBox MB_OK|MB_ICONSTOP "无法创建安装所有权标记，安装未修改。请确认目录可写后重试。"
  Abort

  photoai_preinstall_done:
!macroend

!macro NSIS_HOOK_POSTINSTALL
  ; ExecWait leaves the output variable unchanged when process creation fails.
  ; Seed it with a non-zero sentinel and inspect the error flag immediately so a
  ; missing/broken executable can never be mistaken for a successful self-test.
  StrCpy $PhotoAISelfTestExit -2147483647
  ClearErrors
  ExecWait '"$INSTDIR\${MAINBINARYNAME}.exe" --self-test' $PhotoAISelfTestExit
  ${If} ${Errors}
    StrCpy $PhotoAISelfTestExit -2147483647
  ${EndIf}
  ${If} $PhotoAISelfTestExit <> 0
    ${If} $PhotoAIRollbackActive == 1
      SetOutPath "$TEMP"
      RMDir /r "$INSTDIR"
      ClearErrors
      Rename "$PhotoAIRollbackDir" "$INSTDIR"
      ${If} ${Errors}
        MessageBox MB_OK|MB_ICONSTOP "新版自检失败（错误码 $PhotoAISelfTestExit），自动恢复旧版也失败。旧版仍保留在：$\r$\n$PhotoAIRollbackDir$\r$\n$\r$\n请勿删除该目录。"
        Abort
      ${EndIf}
      WriteRegStr SHCTX "${MANUPRODUCTKEY}" "" "$INSTDIR"
      WriteRegStr SHCTX "${UNINSTKEY}" "DisplayName" "${PRODUCTNAME}"
      WriteRegStr SHCTX "${UNINSTKEY}" "DisplayIcon" "$\"$INSTDIR\${MAINBINARYNAME}.exe$\""
      WriteRegStr SHCTX "${UNINSTKEY}" "InstallLocation" "$\"$INSTDIR$\""
      WriteRegStr SHCTX "${UNINSTKEY}" "UninstallString" "$\"$INSTDIR\uninstall.exe$\""
      ${If} $PhotoAIPreviousDisplayVersion != ""
        WriteRegStr SHCTX "${UNINSTKEY}" "DisplayVersion" "$PhotoAIPreviousDisplayVersion"
      ${EndIf}
      MessageBox MB_OK|MB_ICONSTOP "新版自检失败（错误码 $PhotoAISelfTestExit），已自动恢复原版本。"
    ${Else}
      ; A fresh install has no previous tree to restore. Remove only the
      ; just-created application files, shortcuts and product registrations;
      ; CONTENT_ROOT does not exist yet and is never touched here.
      SetOutPath "$TEMP"
      Delete "$DESKTOP\${PRODUCTNAME}.lnk"
      Delete "$SMPROGRAMS\$AppStartMenuFolder\${PRODUCTNAME}.lnk"
      RMDir "$SMPROGRAMS\$AppStartMenuFolder"
      DeleteRegKey SHCTX "Software\Classes\photoai"
      DeleteRegKey SHCTX "${UNINSTKEY}"
      DeleteRegKey SHCTX "${MANUPRODUCTKEY}"
      Delete "$INSTDIR\${MAINBINARYNAME}.exe"
      Delete "$INSTDIR\uninstall.exe"
      Delete "$INSTDIR\.photoai-install-marker"
      RMDir /r "$INSTDIR\core"
      RMDir /r "$INSTDIR\tools"
      RMDir /r "$INSTDIR\integrations"
      RMDir /r "$INSTDIR\manifests"
      ; Never recursively delete a fresh-install root. A file created after the
      ; empty-directory check remains untouched and causes this non-recursive
      ; removal to fail, which is reported accurately below.
      RMDir "$INSTDIR"
      System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR") i .r0'
      ${If} $0 == -1
        MessageBox MB_OK|MB_ICONSTOP "照片选片安装后自检失败（错误码 $PhotoAISelfTestExit）。本次创建的程序文件已清理，请保留安装日志后重新运行安装程序。"
      ${Else}
        MessageBox MB_OK|MB_ICONSTOP "照片选片安装后自检失败（错误码 $PhotoAISelfTestExit）。仅本次创建的已知程序文件已清理；安装目录仍包含未识别或被占用的内容，因此已安全保留：$\r$\n$INSTDIR"
      ${EndIf}
    ${EndIf}
    Abort
  ${EndIf}
  ${If} $PhotoAIRollbackActive == 1
    SetOutPath "$TEMP"
    RMDir /r /REBOOTOK "$PhotoAIRollbackDir"
  ${EndIf}
!macroend

!macro NSIS_HOOK_PREUNINSTALL
  StrCpy $PhotoAIContentDeleteApproved 0

  ; A manual update must always preserve the external Content Root and plugin.
  ${If} $UpdateMode == 1
    StrCpy $DeleteAppDataCheckboxState 0
    Goto photoai_preuninstall_done
  ${EndIf}

  ; Tauri's stock running-process check is inserted after this hook. Run the
  ; same check here as well so cancelling an uninstall cannot remove the owned
  ; Lightroom plug-in while leaving the desktop application installed.
  !insertmacro CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "${PRODUCTNAME}"

  ; The helper removes the Lightroom plug-in only when its ownership manifest and
  ; every installed file hash still match. User-modified plug-ins are preserved.
  IfFileExists "$INSTDIR\core\worker\PhotoAI.CoreWorker.exe" 0 photoai_plugin_done
  ExecWait '"$INSTDIR\core\worker\PhotoAI.CoreWorker.exe" --remove-owned-lightroom-plugin' $0
  photoai_plugin_done:

  ${If} $DeleteAppDataCheckboxState <> 1
    Goto photoai_preuninstall_done
  ${EndIf}

  ReadRegStr $PhotoAIContentRoot HKCU "Software\PhotoAI" "ContentRoot"
  ${If} $PhotoAIContentRoot == ""
    StrCpy $DeleteAppDataCheckboxState 0
    MessageBox MB_OK|MB_ICONEXCLAMATION "未找到照片选片数据目录记录；为保证安全，本次只卸载程序。"
    Goto photoai_preuninstall_done
  ${EndIf}

  ; The CoreWorker performs the authoritative local-volume, marker, ownership,
  ; layout and path-overlap validation. A missing drive or changed marker fails
  ; closed and leaves all data untouched.
  IfFileExists "$INSTDIR\core\worker\PhotoAI.CoreWorker.exe" 0 photoai_content_invalid
  ExecWait '"$INSTDIR\core\worker\PhotoAI.CoreWorker.exe" --validate-owned-content-root "$PhotoAIContentRoot"' $0
  ${If} $0 <> 0
    Goto photoai_content_invalid
  ${EndIf}

  ${GetSize} "$PhotoAIContentRoot" "/S=0K" $PhotoAIContentSizeKb $1 $2
  IntOp $PhotoAIContentSizeMb $PhotoAIContentSizeKb + 1023
  IntOp $PhotoAIContentSizeMb $PhotoAIContentSizeMb / 1024
  MessageBox MB_YESNO|MB_ICONEXCLAMATION|MB_DEFBUTTON2 "将永久删除照片选片数据目录：$\r$\n$PhotoAIContentRoot$\r$\n$\r$\n约 $PhotoAIContentSizeMb MB。此操作不会删除位于其他目录的 RAW、JPEG 或 XMP。确定继续？" IDYES photoai_content_confirmed
  StrCpy $DeleteAppDataCheckboxState 0
  Goto photoai_preuninstall_done

  photoai_content_invalid:
  StrCpy $DeleteAppDataCheckboxState 0
  MessageBox MB_OK|MB_ICONEXCLAMATION "数据目录未通过 PhotoAI 所有权与路径安全校验；为保证安全，本次只卸载程序。"
  Goto photoai_preuninstall_done

  photoai_content_confirmed:
  StrCpy $PhotoAIContentDeleteApproved 1

  photoai_preuninstall_done:
!macroend

!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $PhotoAIContentDeleteApproved == 1
  ${AndIf} $DeleteAppDataCheckboxState == 1
  ${AndIf} $UpdateMode <> 1
    RMDir /r "$PhotoAIContentRoot"
    ; marker.json may be deleted before a locked child blocks RMDir. Check the
    ; root directory itself so a partial deletion never loses its registry
    ; pointer and remains discoverable for recovery.
    System::Call 'kernel32::GetFileAttributesW(w "$PhotoAIContentRoot") i .r0'
    ${If} $0 != -1
      Goto photoai_content_delete_failed
    ${EndIf}
    DeleteRegValue HKCU "Software\PhotoAI" "ContentRoot"
    DeleteRegKey /ifempty HKCU "Software\PhotoAI"
    Goto photoai_postuninstall_done

    photoai_content_delete_failed:
    MessageBox MB_OK|MB_ICONEXCLAMATION "数据目录未能完整删除，已保留目录指针：$\r$\n$PhotoAIContentRoot"
  ${EndIf}

  photoai_postuninstall_done:
  ; Older builds left the ownership marker and the running uninstaller itself.
  ; Remove the marker immediately and ask Windows to remove the uninstaller and
  ; now-empty program directory after this process exits.
  SetOutPath "$TEMP"
  Delete "$INSTDIR\.photoai-install-marker"
  Delete /REBOOTOK "$INSTDIR\uninstall.exe"
  RMDir /REBOOTOK "$INSTDIR"
!macroend
