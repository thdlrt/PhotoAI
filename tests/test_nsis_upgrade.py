from pathlib import Path
import os
import subprocess
import sys
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Real Windows NSIS upgrade semantics")
ROOT = Path(__file__).parents[1]
COMPILER = ROOT / "desktop/src-tauri/target/.tauri/NSIS/makensis.exe"


def compile_nsis(source: Path, text: str) -> None:
    if not COMPILER.is_file():
        pytest.skip("Bundled NSIS compiler is required")
    source.write_text(text, encoding="utf-8-sig")
    result = subprocess.run([str(COMPILER), "/V2", str(source)], capture_output=True,
                            encoding="utf-8", errors="replace", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("held_path,fail_self_test,installer_inside,content_directory", [
    (None, False, False, "data"), ("data", False, False, "data"), ("root", False, False, "data"),
    ("data", True, False, "data"), ("core", False, False, "data"), ("data", False, True, "data"),
    (None, False, False, "core"), (None, False, False, "root"),
])
def test_real_upgrade_preserves_data_under_directory_locks_and_restores_failed_updates(
    tmp_path, held_path, fail_self_test, installer_inside, content_directory
):
    import winreg
    if not COMPILER.is_file():
        pytest.skip("Bundled NSIS compiler is required")
    install = tmp_path / "照片选片"
    install.mkdir()
    for directory in ["data", "core", "tools", "integrations", "manifests", "licenses"]:
        (install / directory).mkdir()
        (install / directory / "old.txt").write_bytes(b"old payload" if directory != "data" else b"protected project")
    old_program = {name:b"old program" for name in ["PhotoAI.exe", "uninstall.exe", "LICENSE", "THIRD_PARTY_NOTICES.md"]}
    old_program[".photoai-install-marker"] = b"PHOTO_AI_INSTALL/1"
    for name, content in old_program.items():
        (install / name).write_bytes(content)
    (install / "custom.txt").write_bytes(b"user file")
    new_app = tmp_path / "new-app.exe"
    self_test_ran = tmp_path / "self-test-ran.txt"
    compile_nsis(tmp_path / "app.nsi", f'''Unicode true
Name "QA Self Test"
OutFile "{new_app}"
RequestExecutionLevel user
SilentInstall silent
Section
 FileOpen $0 "{self_test_ran}" w
 FileWrite $0 "executed"
 FileClose $0
 SetErrorLevel {1 if fail_self_test else 0}
SectionEnd
''')
    # Isolate every registry read/write and suppress only UI dialogs. The
    # production pre/post macros, filesystem operations and self-test run unchanged.
    registry = "Software\\PhotoAIUpgradeQA-" + uuid.uuid4().hex
    hooks = (ROOT / "desktop/src-tauri/windows/hooks.nsh").read_text(encoding="utf-8")
    hooks = hooks.replace('"Software\\PhotoAI"', f'"{registry}\\Data"')
    hooks = "\n".join("  DetailPrint \"QA failure path\"" if line.lstrip().startswith("MessageBox ") else line
                      for line in hooks.splitlines())
    hook_file = tmp_path / "hooks.nsh"
    hook_file.write_text(hooks, encoding="utf-8-sig")
    harness = install / "installer.exe" if installer_inside else tmp_path / "installer.exe"
    compile_nsis(tmp_path / "installer.nsi", f'''Unicode true
!include LogicLib.nsh
!include FileFunc.nsh
!define MAINBINARYNAME "PhotoAI"
!define PRODUCTNAME "PhotoAI Upgrade QA"
!define UNINSTKEY "{registry}\\Uninstall"
!define MANUPRODUCTKEY "{registry}\\Product"
!macro CheckIfAppIsRunning Executable Product
!macroend
!include "{hook_file}"
Name "Upgrade QA"
OutFile "{harness}"
RequestExecutionLevel user
SilentInstall silent
Var AppStartMenuFolder
Section
 SetRegView 64
 StrCpy $INSTDIR "{install}"
 SetOutPath "$INSTDIR"
 !insertmacro NSIS_HOOK_PREINSTALL
 SetOutPath "$INSTDIR"
 File "/oname=PhotoAI.exe" "{new_app}"
 !insertmacro NSIS_HOOK_POSTINSTALL
 SetErrorLevel 0
SectionEnd
''')
    before = {p.relative_to(install):p.read_bytes() for p in install.rglob("*") if p.is_file()}
    child = None
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, registry + "\\Uninstall", 0,
                               winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as key:
            for name,value in {"InstallLocation":f'"{install}"', "DisplayName":"PhotoAI Upgrade QA", "DisplayVersion":"old"}.items():
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, registry + "\\Data", 0,
                               winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, "ContentRoot", 0, winreg.REG_SZ,
                              str(install if content_directory == "root" else install / content_directory))
        if held_path:
            cwd = install if held_path == "root" else install / held_path
            child = subprocess.Popen([sys.executable, "-c", 'import time; print("ready", flush=True); time.sleep(90)'],
                                     cwd=cwd, stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            assert child.stdout.readline().strip() == "ready"
        outcome = subprocess.run([str(harness), "/S"], cwd=tmp_path, timeout=30)
        failure = fail_self_test or held_path == "core" or content_directory != "data"
        assert (outcome.returncode != 0) == failure
        assert self_test_ran.exists() == (held_path != "core" and content_directory == "data")
        assert (install / "data/old.txt").read_bytes() == b"protected project"
        assert (install / "custom.txt").read_bytes() == b"user file"
        if failure:
            assert all((install / relative).read_bytes() == contents for relative,contents in before.items())
        else:
            assert (install / "PhotoAI.exe").read_bytes() == new_app.read_bytes()
            assert (install / ".photoai-install-marker").read_bytes() == b"PHOTO_AI_INSTALL/1"
            assert all(not (install / name / "old.txt").exists()
                       for name in ["core", "tools", "integrations", "manifests", "licenses"])
        assert not Path(str(install) + ".photoai-previous").exists()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry + "\\Data", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            assert winreg.QueryValueEx(key, "ContentRoot")[0] == str(
                install if content_directory == "root" else install / content_directory
            )
    finally:
        if child:
            child.terminate()
            child.wait(timeout=5)
        for suffix in ["Uninstall", "Data", "Product"]:
            try:
                winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, registry + "\\" + suffix, winreg.KEY_WOW64_64KEY)
            except FileNotFoundError:
                pass
        try:
            winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER, registry, winreg.KEY_WOW64_64KEY)
        except FileNotFoundError:
            pass


@pytest.mark.parametrize("product_registered,unknown_entry,data_name", [
    (True, False, "data"), (True, True, "data"), (False, False, "data"),
    (True, False, "core"), (True, False, "storage/data"),
])
def test_real_reinstall_keeps_registered_data_and_rejects_unrelated_directories(
    tmp_path, product_registered, unknown_entry, data_name
):
    import winreg
    from landscape_culler.content_root import initialize_content_root

    if not COMPILER.is_file():
        pytest.skip("Bundled NSIS compiler is required")
    install = tmp_path / "照片选片"
    install.mkdir()
    layout = initialize_content_root(install / data_name, install_dir=install,
                                    apply_environment=False, persist_registry=False)
    (layout.projects / "old.json").write_bytes(b"manual stars and grouping")
    (layout.models / "weights.bin").write_bytes(b"unchanged cached model")
    (layout.state / "settings.json").write_bytes(b"private settings sentinel")
    if unknown_entry:
        (install / "user-photo.raw").write_bytes(b"original")
    before = {p:p.read_bytes() for p in install.rglob("*") if p.is_file()}
    registry = "Software\\PhotoAIReinstallQA-" + uuid.uuid4().hex
    hooks = (ROOT / "desktop/src-tauri/windows/hooks.nsh").read_text(encoding="utf-8")
    hooks = hooks.replace('"Software\\PhotoAI"', f'"{registry}\\Data"')
    hooks = "\n".join('  DetailPrint "QA failure path"' if line.lstrip().startswith("MessageBox ")
                      else line for line in hooks.splitlines())
    hook_file = tmp_path / "hooks.nsh"
    hook_file.write_text(hooks, encoding="utf-8-sig")
    app = tmp_path / "app.exe"
    compile_nsis(tmp_path / "app.nsi", f'''Unicode true
Name "Reinstall self-test"
OutFile "{app}"
RequestExecutionLevel user
SilentInstall silent
Section
 SetErrorLevel 0
SectionEnd
''')
    installer = tmp_path / "reinstall.exe"
    compile_nsis(tmp_path / "reinstall.nsi", f'''Unicode true
!include LogicLib.nsh
!include FileFunc.nsh
!define MAINBINARYNAME "PhotoAI"
!define PRODUCTNAME "Reinstall QA"
!define UNINSTKEY "{registry}\\Uninstall"
!define MANUPRODUCTKEY "{registry}\\Product"
!macro CheckIfAppIsRunning Executable Product
!macroend
!include "{hook_file}"
Name "Reinstall QA"
OutFile "{installer}"
RequestExecutionLevel user
SilentInstall silent
Var AppStartMenuFolder
Section
 SetRegView 64
 StrCpy $INSTDIR "{install}"
 !insertmacro NSIS_HOOK_PREINSTALL
 SetOutPath "$INSTDIR"
 File "/oname=PhotoAI.exe" "{app}"
 !insertmacro NSIS_HOOK_POSTINSTALL
 SetErrorLevel 0
SectionEnd
''')
    try:
        for suffix, name, value in [("Data", "ContentRoot", str(layout.root))] + (
            [("Product", "", str(install))] if product_registered else []
        ):
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, registry + "\\" + suffix, 0,
                                   winreg.KEY_WRITE | winreg.KEY_WOW64_64KEY) as key:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        outcome = subprocess.run([str(installer), "/S"], cwd=tmp_path, timeout=30)
        expected_success = product_registered and not unknown_entry and data_name != "core"
        assert (outcome.returncode == 0) == expected_success
        assert all(path.read_bytes() == contents for path,contents in before.items())
        assert (install / "PhotoAI.exe").exists() == expected_success
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, registry + "\\Data", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            assert winreg.QueryValueEx(key, "ContentRoot")[0] == str(layout.root)
    finally:
        for suffix in ["Data", "Product", "Uninstall", ""]:
            try:
                winreg.DeleteKeyEx(winreg.HKEY_CURRENT_USER,
                                  registry + ("\\" + suffix if suffix else ""),
                                  winreg.KEY_WOW64_64KEY)
            except FileNotFoundError:
                pass
