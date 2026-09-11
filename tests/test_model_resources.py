from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import landscape_culler.model_resources as resources
from landscape_culler.model_resources import (
    MODEL_SPECS,
    PROFILE_SPECS,
    active_model_profile_readiness,
    active_vlm_model,
    configure_model_profile,
    delete_model_profile,
    delete_model_resource,
    model_resources_status,
)
from landscape_culler.progress import parse_progress_line


def test_transfer_eta_waits_for_a_real_speed_sample(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PHOTO_AI_PROGRESS", "json")
    resources._TransferTelemetry("download", "模型", 1024, resource="model.bin")

    events = [
        event
        for line in capsys.readouterr().out.splitlines()
        if (event := parse_progress_line(line)) is not None
    ]
    telemetry = next(event for event in events if "downloaded_bytes" in event)
    assert telemetry["downloaded_bytes"] == 0
    assert telemetry["bytes_per_second"] == 0
    assert "eta_seconds" not in telemetry


def test_delete_ollama_uses_delete_http_method(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, dict[str, object] | None, float, str | None]] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        resources,
        "_ollama_installed",
        lambda *_args: (True, 123, [], "qwen3-vl:8b-instruct"),
    )
    monkeypatch.setattr(resources, "_ensure_ollama", lambda *_args: None)

    def request(path, payload=None, timeout=5.0, *, method=None):
        calls.append((path, payload, timeout, method))
        return Response()

    monkeypatch.setattr(resources, "_ollama_request", request)

    resources._delete_ollama(
        tmp_path / "models",
        tmp_path / "state",
        MODEL_SPECS["qwen3-vl-8b"],
    )

    assert calls == [
        (
            "/api/delete",
            {"name": "qwen3-vl:8b-instruct"},
            120,
            "DELETE",
        )
    ]


def test_hf_download_source_keeps_mainland_mirror_when_it_is_reachable(monkeypatch) -> None:
    monkeypatch.setattr(resources, "_HF_ENDPOINT_SELECTION", None)
    probed: list[str] = []

    def probe(endpoint, _spec, *, timeout=3.0):
        assert timeout == 3.0
        probed.append(endpoint)
        if "hf-mirror" in endpoint:
            return 1_000_000.0, 0.2
        return 20_000_000.0, 0.02

    monkeypatch.setattr(resources, "_probe_hf_endpoint", probe)

    ordered = resources._hf_endpoint_order(MODEL_SPECS["dinov2-base"])

    assert ordered[0] in resources.HF_DOWNLOAD_ENDPOINTS[:-1]
    assert ordered[-1] == ("Hugging Face 官方源", "https://huggingface.co")
    assert "https://huggingface.co" not in probed


def test_qwen_download_uses_mainland_registry_before_official() -> None:
    assert resources._ollama_pull_sources("qwen3-vl:8b-instruct-q4_K_M") == [
        (
            "国内 Ollama 镜像",
            "ollama.ac.cn/library/qwen3-vl:8b-instruct-q4_K_M",
        ),
        (
            "Ollama 官方源",
            "registry.ollama.ai/library/qwen3-vl:8b-instruct-q4_K_M",
        ),
    ]


def test_hf_download_source_uses_official_only_when_all_mirrors_fail(
    monkeypatch,
) -> None:
    monkeypatch.setattr(resources, "_HF_ENDPOINT_SELECTION", None)

    def probe(endpoint, _spec, *, timeout=3.0):
        raise resources.urllib.error.URLError(f"unreachable: {endpoint}")

    monkeypatch.setattr(resources, "_probe_hf_endpoint", probe)

    ordered = resources._hf_endpoint_order(MODEL_SPECS["dinov2-base"])

    assert ordered[0] == ("Hugging Face 官方源", "https://huggingface.co")


def test_ollama_pull_aggregates_progress_across_layers(
    tmp_path: Path, monkeypatch
) -> None:
    updates: list[int] = []

    class Meter:
        def __init__(self, _phase, _label, total, **_kwargs):
            self.total = total
            self.resumed = 0
            self.last_bytes = 0

        def update(self, current, **_kwargs):
            updates.append(current)

        def finish(self, **_kwargs):
            return None

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            payloads = (
                {"digest": "sha256:a", "completed": 25, "total": 100},
                {"digest": "sha256:a", "completed": 100, "total": 100},
                {"digest": "sha256:b", "completed": 10, "total": 100},
                {"digest": "sha256:b", "completed": 100, "total": 100},
                {"status": "success"},
            )
            return iter(json.dumps(payload).encode() + b"\n" for payload in payloads)

    monkeypatch.setattr(resources, "_ensure_ollama", lambda *_args: None)
    monkeypatch.setattr(resources, "_TransferTelemetry", Meter)
    monkeypatch.setattr(resources, "_ollama_request", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(resources, "_verify_ollama", lambda *_args, **_kwargs: (True, []))

    resources._download_ollama(
        tmp_path / "models",
        tmp_path / "state",
        "qwen3-vl-4b",
    )

    assert updates == [0, 25, 100, 110, 200, 200]


def _install_hf(runtime_root: Path, resource_id: str) -> None:
    spec = MODEL_SPECS[resource_id]
    repo = resources._hf_cache_path(runtime_root, spec["repo_id"])
    snapshot = repo / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "model.safetensors").write_bytes(b"weight")


def _install_managed_hf(
    runtime_root: Path, resource_id: str, weight: bytes = b"weight"
) -> Path:
    spec = MODEL_SPECS[resource_id]
    snapshot = (
        resources._hf_cache_path(runtime_root, spec["repo_id"])
        / "snapshots"
        / spec["revision"]
    )
    snapshot.mkdir(parents=True)
    weight_names = set(spec.get("weight_sha256", {}))
    for filename in spec["files"]:
        path = snapshot / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        if filename in weight_names:
            path.write_bytes(weight)
        elif filename.endswith(".json"):
            path.write_text("{}", encoding="utf-8")
        else:
            path.write_bytes(b"fixture")
    return snapshot


def _install_ollama(runtime_root: Path, name: str, size: int = 1234) -> None:
    executable = resources._ollama_install_dir(runtime_root) / "ollama.exe"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"MZtest-runtime")
    config_content = b"{}"
    layer_content = b"x" * size
    config_digest = hashlib.sha256(config_content).hexdigest()
    layer_digest = hashlib.sha256(layer_content).hexdigest()
    blob_root = resources._ollama_models_root(runtime_root) / "blobs"
    blob_root.mkdir(parents=True, exist_ok=True)
    (blob_root / f"sha256-{config_digest}").write_bytes(config_content)
    (blob_root / f"sha256-{layer_digest}").write_bytes(layer_content)
    manifest = resources._ollama_manifest_candidates(runtime_root, name)[0]
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps(
            {
                "config": {
                    "digest": f"sha256:{config_digest}",
                    "size": 2,
                },
                "layers": [{"digest": f"sha256:{layer_digest}", "size": size}],
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("mirror_succeeds", [False, True])
def test_qwen_pull_keeps_canonical_manifest_after_registry_fallback(
    tmp_path: Path, monkeypatch, mirror_succeeds: bool
) -> None:
    runtime_root = tmp_path / "runtime"
    name = MODEL_SPECS["qwen3-vl-8b"]["ollama_name"]
    calls = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter([b'{"status":"success"}\n'])

    def request(path, payload=None, **_kwargs):
        calls.append((path, payload))
        if path == "/api/pull":
            if "ollama.ac.cn" in payload["name"] and not mirror_succeeds:
                raise resources.urllib.error.URLError("mirror unavailable")
            if not mirror_succeeds:
                _install_ollama(runtime_root, name)
        elif path == "/api/copy":
            _install_ollama(runtime_root, name)
        elif path == "/api/delete":
            raise AssertionError("pull must never delete its just-published model")
        return Response()

    monkeypatch.setattr(resources, "_ensure_ollama", lambda *_args: None)
    monkeypatch.setattr(resources, "_ollama_request", request)
    resources._download_ollama(runtime_root, tmp_path / "state", "qwen3-vl-8b")

    assert resources._verify_ollama(runtime_root, MODEL_SPECS["qwen3-vl-8b"])[0]
    assert sum(path == "/api/pull" for path, _ in calls) == (1 if mirror_succeeds else 2)
    assert sum(path == "/api/copy" for path, _ in calls) == (1 if mirror_succeeds else 0)


def test_qwen_pull_rejects_early_stream_end_even_if_old_manifest_exists(
    tmp_path: Path, monkeypatch
) -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            return iter([b'{"status":"downloading","completed":10,"total":100}\n'])

    monkeypatch.setattr(resources, "_ensure_ollama", lambda *_args: None)
    monkeypatch.setattr(resources, "_ollama_request", lambda *_args, **_kwargs: Response())
    with pytest.raises(RuntimeError, match="连接提前结束"):
        resources._download_ollama(tmp_path, tmp_path / "state", "qwen3-vl-8b")


def test_cached_configuration_does_not_rehash_model_weights(tmp_path: Path, monkeypatch) -> None:
    for resource_id in resources.COMMON_MODEL_IDS:
        _install_hf(tmp_path, resource_id)
    _install_ollama(tmp_path, MODEL_SPECS["qwen3-vl-8b"]["ollama_name"])
    monkeypatch.setattr(resources, "_gpu_status", lambda: {"available": False})
    monkeypatch.setattr(resources, "_sha256_file", lambda _path: pytest.fail("cached weights rehashed"))

    result = configure_model_profile("16gb", tmp_path, tmp_path / "state")

    assert result["settings"]["active_profile"] == "16gb"


def test_inventory_exposes_two_vram_profiles_and_all_active_models(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root = tmp_path / ".runtime"
    data_dir = runtime_root / "data"
    for resource_id in resources.COMMON_MODEL_IDS:
        _install_hf(runtime_root, resource_id)
    _install_ollama(runtime_root, "qwen3-vl:8b-instruct")
    monkeypatch.setattr(
        resources,
        "_gpu_status",
        lambda: {
            "available": True,
            "name": "NVIDIA GeForce RTX 5080",
            "memory_total_mib": 16303,
        },
    )

    status = model_resources_status(runtime_root, data_dir)

    assert status["recommended_profile"] == "16gb"
    assert [item["id"] for item in status["profiles"]] == ["8gb", "16gb"]
    assert len(status["resources"]) == 8
    complete = next(item for item in status["profiles"] if item["id"] == "16gb")
    assert complete["ready"] is True
    assert complete["installed_count"] == complete["model_count"] == 7


def test_configuring_ready_profile_publishes_resolved_ollama_alias(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root = tmp_path / ".runtime"
    data_dir = runtime_root / "data"
    for resource_id in resources.COMMON_MODEL_IDS:
        _install_hf(runtime_root, resource_id)
    _install_ollama(runtime_root, "qwen3-vl:8b-instruct")
    monkeypatch.setattr(resources, "_gpu_status", lambda: {"available": False})

    configured = configure_model_profile("16gb", runtime_root, data_dir)

    assert configured["settings"]["active_profile"] == "16gb"
    assert configured["settings"]["vlm_model"] == "qwen3-vl:8b-instruct"
    assert active_vlm_model(data_dir) == "qwen3-vl:8b-instruct"


def test_delete_huggingface_model_removes_only_its_exact_resource(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root = tmp_path / ".runtime"
    data_dir = runtime_root / "data"
    _install_hf(runtime_root, "dinov2-base")
    _install_hf(runtime_root, "clip-vit-b32")
    dino_path = resources._hf_cache_path(
        runtime_root, MODEL_SPECS["dinov2-base"]["repo_id"]
    )
    clip_path = resources._hf_cache_path(
        runtime_root, MODEL_SPECS["clip-vit-b32"]["repo_id"]
    )
    monkeypatch.setattr(resources, "_gpu_status", lambda: {"available": False})

    result = delete_model_resource("dinov2-base", runtime_root, data_dir)

    assert not dino_path.exists()
    assert clip_path.is_dir()
    by_id = {item["id"]: item for item in result["resources"]}
    assert by_id["dinov2-base"]["installed"] is False
    assert by_id["clip-vit-b32"]["installed"] is True


def test_profiles_use_one_shared_stack_and_distinct_vlm_sizes() -> None:
    light = PROFILE_SPECS["8gb"]
    full = PROFILE_SPECS["16gb"]

    assert light["model_ids"][:-1] == full["model_ids"][:-1]
    assert light["vlm_model_id"] == "qwen3-vl-4b"
    assert full["vlm_model_id"] == "qwen3-vl-8b"


def test_deleting_one_profile_preserves_models_shared_with_installed_other_profile(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root = tmp_path / "models"
    data_dir = tmp_path / "state"
    resources._write_settings(data_dir, "8gb", "qwen3-vl:4b-instruct-q4_K_M")
    before = {
        "resources": [
            {"id": resource_id, "installed": True} for resource_id in MODEL_SPECS
        ],
        "profiles": [
            {"id": "8gb", "ready": True},
            {"id": "16gb", "ready": True},
        ],
    }
    after = {**before, "settings": {"active_profile": "16gb"}}
    statuses = iter([before, after])
    deleted: list[str] = []
    monkeypatch.setattr(resources, "model_resources_status", lambda *_: next(statuses))
    monkeypatch.setattr(
        resources,
        "delete_model_resource",
        lambda resource_id, *_: deleted.append(resource_id),
    )

    result = delete_model_profile("8gb", runtime_root, data_dir)

    assert result is after
    assert deleted == ["qwen3-vl-4b"]
    assert resources.load_model_resource_settings(data_dir)["active_profile"] == "16gb"


def test_active_profile_is_blocked_when_an_ollama_blob_is_missing(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / ".runtime"
    data_dir = runtime_root / "data"
    for resource_id in resources.COMMON_MODEL_IDS:
        _install_hf(runtime_root, resource_id)
    _install_ollama(runtime_root, "qwen3-vl:8b-instruct")
    resources._write_settings(data_dir, "16gb", "qwen3-vl:8b-instruct")
    manifest = resources._ollama_manifest_candidates(
        runtime_root, "qwen3-vl:8b-instruct"
    )[0]
    layer_digest = json.loads(manifest.read_text(encoding="utf-8"))["layers"][0][
        "digest"
    ]
    blob = resources._ollama_blob_path(runtime_root, layer_digest)
    blob.unlink()

    readiness = active_model_profile_readiness(runtime_root, data_dir)

    assert readiness["ready"] is False
    assert readiness["invalid"] == ["qwen3-vl-8b"]
    assert "需修复 1 个" in readiness["message"]


def test_portable_ollama_component_installs_from_verified_archive(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root = tmp_path / ".runtime"
    archive = runtime_root / "downloads" / resources.OLLAMA_ARCHIVE_NAME
    archive.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("ollama.exe", b"MZportable-runtime")
        bundle.writestr("lib/runtime.dll", b"runtime")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    monkeypatch.setattr(resources, "OLLAMA_ARCHIVE_SHA256", digest)
    monkeypatch.setattr(resources, "OLLAMA_ARCHIVE_SIZE", archive.stat().st_size)

    executable = resources._download_ollama_component(runtime_root)

    assert executable == resources._ollama_install_dir(runtime_root) / "ollama.exe"
    assert executable.read_bytes().startswith(b"MZ")
    assert (executable.parent / "lib" / "runtime.dll").is_file()
    assert not archive.exists()


def test_content_root_reserves_random_ollama_endpoint_before_first_probe(
    tmp_path: Path, monkeypatch
) -> None:
    content_root = tmp_path / "PhotoAI"
    runtime_root = content_root / "models"
    data_dir = content_root / "state"
    runtime_root.mkdir(parents=True)
    data_dir.mkdir()
    (content_root / "marker.json").write_text(
        json.dumps({"application": "PhotoAI"}), encoding="utf-8"
    )
    monkeypatch.delenv("PHOTO_AI_OLLAMA_ENDPOINT", raising=False)
    monkeypatch.setattr(resources, "_reserve_loopback_port", lambda: 49177)
    probed: list[str] = []

    class StopProbe(RuntimeError):
        pass

    def reject_probe(*_args, **_kwargs):
        probed.append(resources._ollama_endpoint())
        raise StopProbe

    monkeypatch.setattr(resources, "_ollama_request", reject_probe)

    with pytest.raises(StopProbe):
        resources._ensure_ollama(runtime_root, data_dir)

    assert probed == ["http://127.0.0.1:49177"]
    assert resources._ollama_endpoint() != resources.DEFAULT_OLLAMA_ENDPOINT


def test_content_root_ignores_external_ollama_executable(
    tmp_path: Path, monkeypatch
) -> None:
    content_root = tmp_path / "PhotoAI"
    runtime_root = content_root / "models"
    runtime_root.mkdir(parents=True)
    (content_root / "marker.json").write_text(
        json.dumps({"application": "PhotoAI"}), encoding="utf-8"
    )
    portable = resources._ollama_install_dir(runtime_root) / "ollama.exe"
    portable.parent.mkdir(parents=True)
    portable.write_bytes(b"MZportable")
    external = tmp_path / "system" / "ollama.exe"
    external.parent.mkdir()
    external.write_bytes(b"MZexternal")
    monkeypatch.setenv("PHOTO_AI_OLLAMA", str(external))

    assert resources._ollama_executable(runtime_root) == portable


def test_ollama_component_download_resumes_existing_partial_archive(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PHOTO_AI_PROGRESS", "json")
    runtime_root = tmp_path / "models"
    archive_bytes = None
    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as bundle:
        bundle.writestr("ollama.exe", b"MZportable-runtime")
        bundle.writestr("lib/runtime.dll", b"runtime")
    archive_bytes = source.read_bytes()
    split = len(archive_bytes) // 2
    download = runtime_root / "downloads" / resources.OLLAMA_ARCHIVE_NAME
    part = download.with_suffix(download.suffix + ".part")
    part.parent.mkdir(parents=True)
    part.write_bytes(archive_bytes[:split])
    monkeypatch.setattr(
        resources, "OLLAMA_ARCHIVE_SHA256", hashlib.sha256(archive_bytes).hexdigest()
    )
    monkeypatch.setattr(resources, "OLLAMA_ARCHIVE_SIZE", len(archive_bytes))
    requested_range: list[str | None] = []
    requested_urls: list[str] = []

    class Response:
        status = 206
        headers = {"Content-Length": str(len(archive_bytes) - split)}

        def __init__(self):
            self.remaining = archive_bytes[split:]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def getcode(self):
            return self.status

        def read(self, _size):
            value, self.remaining = self.remaining, b""
            return value

    def urlopen(request, **_kwargs):
        requested_range.append(request.headers.get("Range"))
        requested_urls.append(request.full_url)
        return Response()

    monkeypatch.setattr(resources.urllib.request, "urlopen", urlopen)

    executable = resources._download_ollama_component(runtime_root)

    assert requested_range == [f"bytes={split}-"]
    assert requested_urls == [resources.OLLAMA_ARCHIVE_SOURCES[0][1]]
    assert executable.read_bytes().startswith(b"MZ")
    assert not part.exists()
    events = [
        event
        for line in capsys.readouterr().out.splitlines()
        if (event := parse_progress_line(line)) is not None
    ]
    telemetry = [event for event in events if "resumed_bytes" in event]
    assert telemetry
    assert telemetry[0]["resumed_bytes"] == split
    assert telemetry[-1]["downloaded_bytes"] == len(archive_bytes)
    assert telemetry[-1]["current_resource"] == "ollama.exe"


def test_profile_is_not_activated_when_blob_is_truncated(
    tmp_path: Path, monkeypatch
) -> None:
    runtime_root = tmp_path / ".runtime"
    data_dir = runtime_root / "data"
    for resource_id in resources.COMMON_MODEL_IDS:
        _install_hf(runtime_root, resource_id)
    _install_ollama(runtime_root, "qwen3-vl:8b-instruct", size=128)
    manifest = resources._ollama_manifest_candidates(
        runtime_root, "qwen3-vl:8b-instruct"
    )[0]
    digest = json.loads(manifest.read_text(encoding="utf-8"))["layers"][0]["digest"]
    resources._ollama_blob_path(runtime_root, digest).write_bytes(b"z" * 64)
    monkeypatch.setattr(resources, "_download_ollama", lambda *_args: None)

    try:
        configure_model_profile("16gb", runtime_root, data_dir)
    except RuntimeError as exc:
        assert "模型未安装完整" in str(exc)
    else:
        raise AssertionError("corrupt model profile was activated")

    assert resources.load_model_resource_settings(data_dir)["active_profile"] is None


def test_managed_ollama_component_uses_pinned_executable_and_cleans_archive(
    tmp_path: Path, monkeypatch
) -> None:
    content_root = tmp_path / "PhotoAI"
    runtime_root = content_root / "models"
    runtime_root.mkdir(parents=True)
    (content_root / "marker.json").write_text(
        json.dumps({"application": "PhotoAI"}), encoding="utf-8"
    )
    archive = content_root / "downloads" / resources.OLLAMA_ARCHIVE_NAME
    archive.parent.mkdir()
    executable_bytes = b"MZmanaged-ollama"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("ollama.exe", executable_bytes)
    monkeypatch.setattr(
        resources,
        "OLLAMA_ARCHIVE_SHA256",
        hashlib.sha256(archive.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(resources, "OLLAMA_ARCHIVE_SIZE", archive.stat().st_size)
    monkeypatch.setattr(
        resources,
        "OLLAMA_EXECUTABLE_SHA256",
        hashlib.sha256(executable_bytes).hexdigest(),
    )
    version_checks: list[Path] = []
    monkeypatch.setattr(
        resources,
        "_ollama_version_matches",
        lambda path: version_checks.append(path) is None,
    )

    executable = resources._download_ollama_component(runtime_root)

    assert version_checks == [executable]
    assert resources._ollama_component_status(runtime_root)["verified"] is True
    assert not archive.exists()


def test_profile_repair_redownloads_empty_managed_hf_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    content_root = tmp_path / "PhotoAI"
    runtime_root = content_root / "models"
    data_dir = content_root / "state"
    runtime_root.mkdir(parents=True)
    data_dir.mkdir()
    (content_root / "marker.json").write_text(
        json.dumps({"application": "PhotoAI"}), encoding="utf-8"
    )
    good_weight = b"good"
    bad_weight = b""
    snapshot = _install_managed_hf(runtime_root, "dinov2-base", good_weight)
    weight_path = snapshot / "model.safetensors"
    weight_path.write_bytes(bad_weight)
    monkeypatch.setitem(
        MODEL_SPECS["dinov2-base"]["weight_sha256"],
        "model.safetensors",
        hashlib.sha256(good_weight).hexdigest(),
    )

    _install_ollama(runtime_root, MODEL_SPECS["qwen3-vl-4b"]["ollama_name"])
    executable = resources._ollama_install_dir(runtime_root) / "ollama.exe"
    monkeypatch.setattr(
        resources,
        "OLLAMA_EXECUTABLE_SHA256",
        hashlib.sha256(executable.read_bytes()).hexdigest(),
    )
    manifest = resources._ollama_manifest_candidates(
        runtime_root, MODEL_SPECS["qwen3-vl-4b"]["ollama_name"]
    )[0]
    monkeypatch.setitem(
        MODEL_SPECS["qwen3-vl-4b"],
        "manifest_digest",
        f"sha256:{hashlib.sha256(manifest.read_bytes()).hexdigest()}",
    )
    monkeypatch.setitem(
        PROFILE_SPECS,
        "8gb",
        {
            **PROFILE_SPECS["8gb"],
            "model_ids": ["dinov2-base", "qwen3-vl-4b"],
        },
    )
    downloads: list[str] = []

    def restore_hf(root: Path, resource_id: str) -> None:
        downloads.append(resource_id)
        weight_path.write_bytes(good_weight)

    monkeypatch.setattr(resources, "_download_hf", restore_hf)

    result = configure_model_profile(
        "8gb", runtime_root, data_dir, publish_settings=False
    )

    assert downloads == ["dinov2-base"]
    assert weight_path.read_bytes() == good_weight
    assert (
        next(item for item in result["profiles"] if item["id"] == "8gb")["ready"]
        is True
    )


def test_hf_repair_keeps_good_files_and_download_resume_data(tmp_path):
    spec = MODEL_SPECS["dinov2-base"]
    repository = resources._hf_cache_path(tmp_path, spec["repo_id"])
    snapshot = repository / "snapshots" / spec["revision"]
    snapshot.mkdir(parents=True)
    (snapshot / "config.json").write_text("{}", encoding="utf-8")
    (snapshot / "model.safetensors").write_bytes(b"")
    partial = repository / "blobs" / "weight.incomplete"
    partial.parent.mkdir()
    partial.write_bytes(b"downloaded prefix")
    resources._prepare_hf_resource_repair(tmp_path, spec)
    assert partial.read_bytes() == b"downloaded prefix"
    assert (snapshot / "config.json").read_text(encoding="utf-8") == "{}"
    assert not (snapshot / "model.safetensors").exists()
