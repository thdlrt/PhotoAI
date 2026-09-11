from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_all_core_cli_implementations_are_packaged_and_self_tested() -> None:
    from landscape_culler.core_worker import CORE_COMMAND_MODULES

    cli = ast.parse((PROJECT_ROOT / "src/landscape_culler/cli.py").read_text(encoding="utf-8"))
    implementations = {
        "landscape_culler" + node.args[0].value
        for node in ast.walk(cli)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_command_callable"
    }
    ai_only = {
        "landscape_culler.scoring", "landscape_culler.develop_worker",
        "landscape_culler.style_worker", "landscape_culler.lut_export_worker",
    }
    assert implementations - ai_only == set(CORE_COMMAND_MODULES)
    spec = (PROJECT_ROOT / "packaging/PhotoAI.spec").read_text(encoding="utf-8")
    worker_spec = spec.split("worker_analysis = core_analysis(", 1)[1]
    for module_name in CORE_COMMAND_MODULES:
        assert f'"{module_name}"' in worker_spec


def test_release_gate_executes_cleanup_not_only_parser_self_test() -> None:
    gate = (PROJECT_ROOT / "packaging/verify-release.py").read_text(encoding="utf-8")
    assert "verify_xmp_cleanup_runtime(executable, scratch)" in gate
    assert '"xmp-cleanup-execute"' in gate
    assert 'result.get("deleted_count") != 2' in gate


def test_pyinstaller_spec_builds_only_two_lightweight_console_sidecars() -> None:
    spec = (PROJECT_ROOT / "packaging/PhotoAI.spec").read_text(encoding="utf-8")

    assert "collect_all" not in spec
    assert 'collect_submodules("landscape_culler")' not in spec
    assert 'name="PhotoAI.Service"' in spec
    assert 'name="PhotoAI.CoreWorker"' in spec
    assert '"landscape_culler.ai_runtime"' in spec
    assert '"landscape_culler.model_resources"' in spec
    assert '"landscape_culler.offline_bundle"' in spec
    assert spec.count("console=True") == 2
    assert "version_info_worker.txt" in spec
    worker_version = (PROJECT_ROOT / "packaging/version_info_worker.txt").read_text(
        encoding="utf-8"
    )
    assert "PhotoAI.CoreWorker.exe" in worker_version
    for package in (
        "OpenColorIO",
        "cv2",
        "numpy",
        "pyiqa",
        "pyvips",
        "rawpy",
        "torch",
        "torchvision",
        "transformers",
    ):
        assert f'"{package}"' in spec

    for entrypoint in ("service_entry.py", "core_worker_entry.py"):
        wrapper = (PROJECT_ROOT / "packaging" / entrypoint).read_text(
            encoding="utf-8"
        )
        assert "sys.argv" not in wrapper
        assert '"--self-test"' not in wrapper
        assert "raise SystemExit(main())" in wrapper


def test_release_orchestrator_stages_reviewed_resources_only() -> None:
    script = (PROJECT_ROOT / "packaging/build-windows.ps1").read_text(
        encoding="utf-8-sig"
    )

    assert "packaging\\dist\\core" in script
    assert "$StageTools = Join-Path $StageRoot 'tools'" in script
    assert "$StageManifests = Join-Path $StageRoot 'manifests'" in script
    assert "$StageAiWorker = Join-Path $StageManifests 'ai-worker'" in script
    assert "verify-release.py" in script
    assert "build-desktop.ps1" in script
    assert "$StagePlugin = Join-Path $StageIntegrations 'photo-ai-lightroom.lrplugin'" in script
    assert "$_.Name -ne 'bridge-path.txt'" in script
    assert "Copy-Item -LiteralPath (Join-Path $ProjectRoot 'integrations')" not in script
    assert "Copy-Item -LiteralPath (Join-Path $ProjectRoot '.runtime')" not in script
    for field in (
        "api_version",
        "service_protocol",
        "worker_protocol",
        "ai_engine_version",
        "managed_python_version",
    ):
        assert field in script

    desktop_script = (PROJECT_ROOT / "packaging/build-desktop.ps1").read_text(
        encoding="utf-8-sig"
    )
    assert "desktop-self-test" in desktop_script
    assert "PhotoAI.exe')" in desktop_script
    assert "foreach ($Name in @('core', 'tools', 'integrations', 'manifests'))" in desktop_script

    rust = (PROJECT_ROOT / "desktop/src-tauri/src/main.rs").read_text(encoding="utf-8")
    assert "pick_offline_bundle" in rust
    assert 'add_filter("PhotoAI 离线资源包", &["photoai-offline"])' in rust


def test_nsis_hooks_fail_closed_around_owned_data_and_plugin_removal() -> None:
    hooks = (PROJECT_ROOT / "desktop/src-tauri/windows/hooks.nsh").read_text(
        encoding="utf-8"
    )
    tauri = json.loads(
        (PROJECT_ROOT / "desktop/src-tauri/tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )

    assert tauri["bundle"]["windows"]["nsis"]["installerHooks"] == "./windows/hooks.nsh"
    assert '"$INSTDIR\\${MAINBINARYNAME}.exe" --self-test' in hooks
    assert "--validate-owned-content-root" in hooks
    assert "--remove-owned-lightroom-plugin" in hooks
    assert "GetFileAttributesW" in hooks
    assert "PhotoAIContentDeleteApproved" in hooks
    assert "PhotoAIRollbackActive" in hooks
    assert "PhotoAISelfTestExit" in hooks
    assert "PhotoAIPreviousInstallLocation" in hooks
    assert "PhotoAIPreviousDisplayName" in hooks
    assert "PHOTO_AI_INSTALL/1" in hooks
    assert 'IfFileExists "$PhotoAIRollbackDir\\."' in hooks
    assert 'Rename "$INSTDIR" "$PhotoAIRollbackDir"' in hooks
    assert 'Rename "$PhotoAIRollbackDir" "$INSTDIR"' in hooks
    assert 'DeleteRegKey SHCTX "Software\\Classes\\photoai"' in hooks
    assert 'RMDir /r "$INSTDIR"' in hooks
    assert "$UpdateMode == 1" in hooks
    assert "MB_YESNO|MB_ICONEXCLAMATION|MB_DEFBUTTON2" in hooks

    process_check = hooks.index("!insertmacro CheckIfAppIsRunning")
    plugin_removal = hooks.index("--remove-owned-lightroom-plugin")
    content_validation = hooks.index("--validate-owned-content-root")
    recursive_delete = hooks.index('RMDir /r "$PhotoAIContentRoot"')
    assert process_check < plugin_removal < content_validation < recursive_delete


def test_nsis_update_requires_exact_owned_install_and_self_test_fails_closed() -> None:
    hooks = (PROJECT_ROOT / "desktop/src-tauri/windows/hooks.nsh").read_text(
        encoding="utf-8"
    )

    update_validation = hooks.index("photoai_preinstall_validate_update:")
    running_check = hooks.index("!insertmacro CheckIfAppIsRunning")
    backup = hooks.index('Rename "$INSTDIR" "$PhotoAIRollbackDir"')
    assert update_validation < running_check < backup
    assert (
        'StrCmp $PhotoAIPreviousInstallLocation "$\\"$INSTDIR$\\"" 0 '
        "photoai_preinstall_not_owned"
    ) in hooks
    assert (
        'StrCmp $PhotoAIPreviousDisplayName "${PRODUCTNAME}" 0 '
        "photoai_preinstall_not_owned"
    ) in hooks
    assert 'IfFileExists "$INSTDIR\\uninstall.exe" 0 photoai_preinstall_not_owned' in hooks
    assert (
        'IfFileExists "$INSTDIR\\.photoai-install-marker" 0 '
        "photoai_preinstall_not_owned"
    ) in hooks
    assert (
        'StrCmp $PhotoAIInstallMarkerContents "PHOTO_AI_INSTALL/1" 0 '
        "photoai_preinstall_not_owned"
    ) in hooks

    sentinel = hooks.index("StrCpy $PhotoAISelfTestExit -2147483647")
    clear_errors = hooks.index("ClearErrors", sentinel)
    exec_wait = hooks.index("ExecWait", sentinel)
    error_check = hooks.index("${If} ${Errors}", exec_wait)
    exit_check = hooks.index("${If} $PhotoAISelfTestExit <> 0", error_check)
    assert sentinel < clear_errors < exec_wait < error_check < exit_check


def test_nsis_fresh_install_requires_empty_root_and_cleans_known_payload_only() -> None:
    hooks = (PROJECT_ROOT / "desktop/src-tauri/windows/hooks.nsh").read_text(
        encoding="utf-8"
    )

    assert 'FindFirst $0 $1 "$INSTDIR\\*"' in hooks
    assert 'FileWrite $0 "PHOTO_AI_INSTALL/1"' in hooks
    marker_write = hooks.index('FileWrite $0 "PHOTO_AI_INSTALL/1"')
    fresh_scan = hooks.index('FindFirst $0 $1 "$INSTDIR\\*"', marker_write)
    assert marker_write < fresh_scan < hooks.index("!macro NSIS_HOOK_POSTINSTALL")
    assert (
        'StrCmp $1 ".photoai-install-marker" photoai_preinstall_fresh_next'
        in hooks
    )
    assert "photoai_preinstall_recover_stale:" in hooks
    assert (
        'StrCmp $1 "uninstall.exe" photoai_preinstall_recover_next' in hooks
    )
    assert (
        'StrCmp $PhotoAIInstallMarkerContents "PHOTO_AI_INSTALL/1" 0 '
        "photoai_preinstall_fresh_not_empty"
    ) in hooks
    assert 'Delete "$INSTDIR\\uninstall.exe"' in hooks

    postinstall = hooks[
        hooks.index("!macro NSIS_HOOK_POSTINSTALL") : hooks.index(
            "!macro NSIS_HOOK_PREUNINSTALL"
        )
    ]
    assert postinstall.count('RMDir /r "$INSTDIR"') == 1
    for relative in ("core", "tools", "integrations", "manifests"):
        assert f'RMDir /r "$INSTDIR\\{relative}"' in postinstall
    assert 'Delete "$INSTDIR\\${MAINBINARYNAME}.exe"' in postinstall
    assert 'Delete "$INSTDIR\\uninstall.exe"' in postinstall
    assert 'Delete "$INSTDIR\\.photoai-install-marker"' in postinstall
    assert 'RMDir "$INSTDIR"' in postinstall
    assert 'GetFileAttributesW(w "$INSTDIR")' in postinstall

    install_hooks = hooks[
        hooks.index("!macro NSIS_HOOK_PREINSTALL") : hooks.index(
            "!macro NSIS_HOOK_PREUNINSTALL"
        )
    ]
    assert "$PhotoAIContentRoot" not in install_hooks

    postuninstall = hooks[hooks.index("!macro NSIS_HOOK_POSTUNINSTALL") :]
    assert 'Delete "$INSTDIR\\.photoai-install-marker"' in postuninstall
    assert 'Delete /REBOOTOK "$INSTDIR\\uninstall.exe"' in postuninstall
    assert 'RMDir /REBOOTOK "$INSTDIR"' in postuninstall


def test_all_release_surfaces_share_product_version() -> None:
    tauri = json.loads(
        (PROJECT_ROOT / "desktop/src-tauri/tauri.conf.json").read_text(
            encoding="utf-8"
        )
    )
    npm = json.loads(
        (PROJECT_ROOT / "desktop/package.json").read_text(encoding="utf-8")
    )
    cargo = (PROJECT_ROOT / "desktop/src-tauri/Cargo.toml").read_text(
        encoding="utf-8"
    )
    version_module = (PROJECT_ROOT / "src/landscape_culler/version.py").read_text(
        encoding="utf-8"
    )
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )

    cargo_version = re.search(r'^version\s*=\s*"([^"]+)"', cargo, re.MULTILINE)
    product_version = re.search(
        r'^PRODUCT_VERSION\s*=\s*"([^"]+)"', version_module, re.MULTILINE
    )
    assert cargo_version is not None
    assert product_version is not None
    assert {
        tauri["version"],
        npm["version"],
        cargo_version.group(1),
        product_version.group(1),
    } == {"0.9.0-beta.1"}
    assert pyproject["project"]["version"] == "0.9.0b1"


def test_release_manifests_are_fixed_and_hashed() -> None:
    lock = (PROJECT_ROOT / "packaging/resources/ai-requirements.lock").read_text(
        encoding="utf-8"
    )
    requirements = [
        line
        for line in lock.splitlines()
        if line and not line.startswith((" ", "#", "--"))
    ]
    assert requirements
    assert all("==" in requirement for requirement in requirements)
    assert "--hash=sha256:" in lock

    manifest = json.loads(
        (PROJECT_ROOT / "packaging/resources/model-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["huggingface"]
    assert manifest["ollama"]
    for model in manifest["huggingface"].values():
        assert re.fullmatch(r"[0-9a-f]{40}", model["revision"])
        assert all(
            re.fullmatch(r"[0-9a-f]{64}", digest)
            for digest in model["weight_files"].values()
        )
    for model in manifest["ollama"].values():
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", model["manifest_digest"])
