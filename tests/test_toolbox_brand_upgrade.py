from pathlib import Path
import os
import re
import subprocess
import uuid

import pytest


ROOT = Path(__file__).parents[1]
COMPILER = ROOT / "desktop/src-tauri/target/.tauri/NSIS/makensis.exe"


@pytest.mark.skipif(os.name != "nt", reason="Windows NSIS ownership validation")
@pytest.mark.parametrize("display_name,marker,accepted", [
    ("照片选片", "PHOTO_AI_INSTALL/1", True),
    ("摄影后期工具箱", "PHOTO_AI_INSTALL/1", True),
    ("Unrelated App", "PHOTO_AI_INSTALL/1", False),
    ("照片选片", "NOT_OWNED", False),
])
def test_brand_upgrade_accepts_owned_legacy_install_only(tmp_path, display_name, marker, accepted):
    import winreg

    if not COMPILER.is_file():
        pytest.skip("NSIS compiler unavailable")
    install = tmp_path / "existing-install"
    install.mkdir()
    for name, value in {"PhotoAI.exe": b"original app", "uninstall.exe": b"original uninstaller",
                        ".photoai-install-marker": marker.encode()}.items():
        (install / name).write_bytes(value)
    before = {p: p.read_bytes() for p in install.iterdir()}
    registry = "Software\\PhotoAIToolboxBrandQA-" + uuid.uuid4().hex
    hooks = (ROOT / "desktop/src-tauri/windows/hooks.nsh").read_text(encoding="utf-8")
    hooks = hooks.replace('"Software\\PhotoAI"', f'"{registry}\\Data"')
    hooks = "\n".join('  DetailPrint "QA validation stopped"' if line.lstrip().startswith("MessageBox ") else line
                      for line in hooks.splitlines())
    hook_file = tmp_path / "hooks.nsh"
    hook_file.write_text(hooks, encoding="utf-8-sig")
    success = tmp_path / "accepted.txt"
    executable = tmp_path / "brand-check.exe"
    source = tmp_path / "brand-check.nsi"
    source.write_text(f'''Unicode true
!include LogicLib.nsh
!include FileFunc.nsh
!define MAINBINARYNAME "PhotoAI"
!define PRODUCTNAME "摄影后期工具箱"
!define UNINSTKEY "{registry}\\Uninstall"
!define MANUPRODUCTKEY "{registry}\\Product"
!macro CheckIfAppIsRunning Executable Product
!macroend
!include "{hook_file}"
Name "Brand Compatibility QA"
OutFile "{executable}"
RequestExecutionLevel user
SilentInstall silent
Var AppStartMenuFolder
Section
 SetRegView 64
 StrCpy $INSTDIR "{install}"
 !insertmacro NSIS_HOOK_PREINSTALL
 FileOpen $0 "{success}" w
 FileWrite $0 "accepted"
 FileClose $0
 StrCpy $PhotoAIRestoreFailed 0
 !insertmacro PhotoAIProgramPayload PhotoAIRestoreProgramEntry
 RMDir "$PhotoAIRollbackDir"
 SetErrorLevel 0
SectionEnd
''', encoding="utf-8-sig")
    compiled = subprocess.run([str(COMPILER), "/V2", str(source)], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=30)
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, registry + "\\Uninstall", 0,
                               winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as key:
            for name, value in {"InstallLocation": f'"{install}"', "DisplayName": display_name,
                                "DisplayVersion": "0.9.0-beta.4"}.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        result = subprocess.run([str(executable), "/S"], cwd=tmp_path, timeout=15)
        assert (result.returncode == 0) is accepted
        assert success.exists() is accepted
        assert all(path.read_bytes() == value for path, value in before.items())
    finally:
        for suffix in ("Uninstall", "Data", "Product"):
            try:
                winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, registry + "\\" + suffix, winreg.KEY_WOW64_64KEY)
            except FileNotFoundError:
                pass
        winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, registry, winreg.KEY_WOW64_64KEY)


@pytest.mark.skipif(os.name != "nt", reason="Windows NSIS registry compatibility")
def test_template_reopens_legacy_install_and_updates_same_registry_entry(tmp_path):
    import winreg
    from test_nsis_upgrade import compile_nsis

    if not COMPILER.is_file():
        pytest.skip("NSIS compiler unavailable")
    template = (ROOT / "desktop/src-tauri/windows/installer-template.nsi").read_text(encoding="utf-8")
    uninstall = re.search(r'^!define UNINSTKEY "([^"]+)"', template, re.M).group(1)
    product = re.search(r'^!define MANUPRODUCTKEY "([^"]+)"', template, re.M).group(1)
    assert uninstall == r"Software\Microsoft\Windows\CurrentVersion\Uninstall\照片选片"
    assert product == r"${MANUKEY}\照片选片"
    restore = re.search(r"Function RestorePreviousInstallLocation\n.*?\nFunctionEnd", template, re.S).group(0)
    registry = "Software\\PhotoAITemplateQA-" + uuid.uuid4().hex
    uninstall = uninstall.replace(r"Software\Microsoft\Windows\CurrentVersion", registry)
    # Both the old and new display names resolve to the original stable keys.
    registry_uninstall = registry + "\\Uninstall\\照片选片"
    registry_product = registry + "\\Manufacturer\\照片选片"
    install = tmp_path / "old-location"
    install.mkdir()
    for filename in ("PhotoAI.exe", "uninstall.exe"):
        (install / filename).write_bytes(b"owned old binary")
    (install / ".photoai-install-marker").write_bytes(b"PHOTO_AI_INSTALL/1")
    (install / "keep-user-file.txt").write_bytes(b"untouched")
    before = {path: path.read_bytes() for path in install.iterdir()}
    hooks = (ROOT / "desktop/src-tauri/windows/hooks.nsh").read_text(encoding="utf-8")
    hooks = hooks.replace('"Software\\PhotoAI"', f'"{registry}\\Data"')
    hooks = "\n".join('  DetailPrint "QA rejected"' if line.lstrip().startswith("MessageBox ") else line
                      for line in hooks.splitlines())
    hook_file = tmp_path / "hooks.nsh"
    hook_file.write_text(hooks, encoding="utf-8-sig")
    exe = tmp_path / "template-check.exe"
    compile_nsis(tmp_path / "template-check.nsi", f'''Unicode true
!include LogicLib.nsh
!include FileFunc.nsh
!define MAINBINARYNAME "PhotoAI"
!define PRODUCTNAME "摄影后期工具箱"
!define MANUKEY "{registry}\\Manufacturer"
!define UNINSTKEY "{uninstall}"
!define MANUPRODUCTKEY "{product}"
!macro CheckIfAppIsRunning Executable Product
!macroend
!include "{hook_file}"
Name "Template Registry QA"
OutFile "{exe}"
RequestExecutionLevel user
SilentInstall silent
Var AppStartMenuFolder
{restore}
Section
 SetRegView 64
 StrCpy $INSTDIR "{tmp_path / 'wrong-new-default'}"
 Call RestorePreviousInstallLocation
 !insertmacro NSIS_HOOK_PREINSTALL
 WriteRegStr SHCTX "${{UNINSTKEY}}" "DisplayName" "${{PRODUCTNAME}}"
 WriteRegStr SHCTX "${{MANUPRODUCTKEY}}" "" "$INSTDIR"
 StrCpy $PhotoAIRestoreFailed 0
 !insertmacro PhotoAIProgramPayload PhotoAIRestoreProgramEntry
 RMDir "$PhotoAIRollbackDir"
 SetErrorLevel 0
SectionEnd
''')
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, registry_uninstall, 0,
                               winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as key:
            for name, value in {"InstallLocation": f'"{install}"', "DisplayName": "照片选片", "DisplayVersion": "0.9.0-beta.4"}.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, registry_product, 0,
                               winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(install))
        result = subprocess.run([str(exe), "/S"], cwd=tmp_path, timeout=15)
        assert result.returncode == 0
        assert all(path.read_bytes() == data for path, data in before.items())
        assert not (tmp_path / "wrong-new-default").exists()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry_uninstall, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            assert winreg.QueryValueEx(key, "DisplayName")[0] == "摄影后期工具箱"
        with pytest.raises(FileNotFoundError):
            winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry + "\\Uninstall\\摄影后期工具箱", 0,
                           winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    finally:
        for suffix in ("Uninstall\\照片选片", "Manufacturer\\照片选片", "Uninstall", "Manufacturer", "Data"):
            try:
                winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, registry + "\\" + suffix, winreg.KEY_WOW64_64KEY)
            except FileNotFoundError:
                pass
        winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, registry, winreg.KEY_WOW64_64KEY)
