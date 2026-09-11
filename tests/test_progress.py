from __future__ import annotations

from pathlib import Path

import numpy as np

from landscape_culler.features import FeatureExtractor
from landscape_culler.general_aesthetic import GeneralAestheticScorer
from landscape_culler.group_critic import OllamaGroupCritic
from landscape_culler.progress import (
    PROGRESS_PREFIX,
    emit_progress,
    parse_progress_line,
    phase_end,
    phase_start,
)
from landscape_culler.web import (
    JobManager,
    _apply_job_progress,
    _complete_job_progress,
    _initial_job_progress,
    _job_progress_plan,
)


def _events(output: str) -> list[dict]:
    return [
        event
        for line in output.splitlines()
        if (event := parse_progress_line(line)) is not None
    ]


def test_progress_protocol_is_structured_and_rejects_malformed(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PHOTO_AI_PROGRESS", "json")
    phase_start("photos", "读取照片", 4, unit="张")
    emit_progress("photos", "读取照片", 2, 4, unit="张", cached=1)
    phase_end("photos", "读取照片", 4, unit="张", cached=1)

    raw = capsys.readouterr().out
    assert "读取照片" not in raw
    assert "\\u8bfb\\u53d6\\u7167\\u7247" in raw
    events = _events(raw)
    assert [event["event"] for event in events] == [
        "phase_start",
        "progress",
        "phase_end",
    ]
    assert events[1] == {
        "v": 1,
        "event": "progress",
        "phase": "photos",
        "label": "读取照片",
        "current": 2,
        "total": 4,
        "unit": "张",
        "cached": 1,
    }
    assert parse_progress_line(f"{PROGRESS_PREFIX}not-json") is None
    assert parse_progress_line("普通日志") is None


def test_progress_protocol_preserves_install_telemetry_and_clears_stale_fields(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PHOTO_AI_PROGRESS", "json")
    emit_progress(
        "dependencies",
        "安装固定 AI 依赖",
        0,
        1,
        unit="项",
        detail="正在下载 Torch",
        current_resource="torch",
        downloaded_bytes=128,
        total_bytes=1024,
        bytes_per_second=64.5,
        eta_seconds=13.9,
        resumed_bytes=32,
        elapsed_seconds=2.0,
        heartbeat_at="2026-09-02T00:00:00+00:00",
    )
    event = _events(capsys.readouterr().out)[0]
    assert event["current_resource"] == "torch"
    assert event["downloaded_bytes"] == 128
    assert event["total_bytes"] == 1024
    assert event["bytes_per_second"] == 64.5
    assert event["eta_seconds"] == 13.9
    assert event["resumed_bytes"] == 32
    assert event["elapsed_seconds"] == 2.0

    plan = _job_progress_plan("model_download", {"profile_id": "8gb", "managed_engine": True})
    job = {
        "kind": "model_download",
        "context": {"profile_id": "8gb", "managed_engine": True},
        "progress": _initial_job_progress(plan),
    }
    _apply_job_progress(job, event)
    assert job["progress"]["bytes_per_second"] == 64.5
    assert job["message"] == "正在下载 Torch"

    _apply_job_progress(
        job,
        {
            "event": "phase_start",
            "phase": "worker",
            "label": "安装 AI Worker",
            "current": 0,
            "total": 1,
            "unit": "项",
            "cached": 0,
        },
    )
    for key in (
        "detail",
        "current_resource",
        "downloaded_bytes",
        "total_bytes",
        "bytes_per_second",
        "eta_seconds",
        "resumed_bytes",
        "elapsed_seconds",
        "heartbeat_at",
    ):
        assert key not in job["progress"]


def test_deep_job_progress_has_nodes_and_never_moves_backwards() -> None:
    context = {"mode": "deep"}
    plan = _job_progress_plan("score", context)
    job = {"kind": "score", "context": context, "progress": _initial_job_progress(plan)}

    _apply_job_progress(
        job,
        {
            "event": "phase_end",
            "phase": "previews",
            "label": "读取照片",
            "current": 83,
            "total": 83,
            "unit": "张",
            "cached": 83,
        },
    )
    first_percent = job["progress"]["overall_percent"]
    changed, boundary = _apply_job_progress(
        job,
        {
            "event": "progress",
            "phase": "local_vlm",
            "label": "构图组内评审",
            "current": 20,
            "total": 40,
            "unit": "张",
            "cached": 0,
        },
    )

    assert changed is True and boundary is False
    assert job["progress"]["overall_percent"] > first_percent
    assert job["progress"]["stage_percent"] == 50.0
    statuses = {node["key"]: node["status"] for node in job["progress"]["nodes"]}
    assert statuses["aesthetic"] == "completed"
    assert statuses["local_vlm"] == "active"
    assert statuses["global_vlm"] == "pending"

    # A delayed earlier event cannot make the visible total percentage regress.
    before = job["progress"]["overall_percent"]
    _apply_job_progress(
        job,
        {
            "event": "progress",
            "phase": "features",
            "label": "读取相似特征",
            "current": 1,
            "total": 83,
            "unit": "张",
            "cached": 0,
        },
    )
    assert job["progress"]["overall_percent"] == before
    assert job["progress"]["stage_key"] == "local_vlm"

    _complete_job_progress(job)
    assert job["progress"]["overall_percent"] == 100.0
    assert all(node["status"] == "completed" for node in job["progress"]["nodes"])


def test_raw_jpeg_jobs_expose_verify_move_and_finalize_nodes() -> None:
    execute = _job_progress_plan("raw_jpeg_execute", {})
    rollback = _job_progress_plan("raw_jpeg_rollback", {})
    assert [node["key"] for node in execute] == ["verify", "recycle", "finalize"]
    assert [node["key"] for node in rollback] == ["verify", "restore", "finalize"]
    assert sum(node["weight"] for node in execute) == 100
    assert sum(node["weight"] for node in rollback) == 100


def test_xmp_cleanup_jobs_expose_verify_delete_and_finalize_nodes() -> None:
    execute = _job_progress_plan("xmp_cleanup_execute", {})
    rollback = _job_progress_plan("xmp_cleanup_rollback", {})
    assert [node["key"] for node in execute] == ["verify", "delete", "finalize"]
    assert execute[1]["label"] == "永久删除 XMP"
    assert [node["key"] for node in rollback] == ["verify", "restore", "finalize"]
    assert sum(node["weight"] for node in execute) == 100
    assert sum(node["weight"] for node in rollback) == 100


def test_style_jobs_show_lightroom_launch_before_scene_or_render() -> None:
    recommendation = _job_progress_plan("style_recommend", {})
    preview = _job_progress_plan("style_preview", {"requires_lightroom": True})
    lut_preview = _job_progress_plan(
        "style_preview",
        {"requires_lightroom": False, "lut_id": "cube-test"},
    )

    assert [node["key"] for node in recommendation] == [
        "launch",
        "presets",
        "representative",
        "scene",
        "recall",
        "process",
        "collect",
        "rerank",
        "finalize",
    ]
    assert [node["key"] for node in preview] == [
        "launch",
        "process",
        "finalize",
    ]
    assert [node["key"] for node in lut_preview] == ["lut", "finalize"]
    assert all(
        sum(node["weight"] for node in plan) == 100
        for plan in (recommendation, preview, lut_preview)
    )

    job = {
        "kind": "style_recommend",
        "context": {"lightroom_state": "stale"},
        "progress": _initial_job_progress(recommendation),
    }
    assert job["progress"]["stage_key"] == "launch"
    assert job["progress"]["stage_label"] == "等待 Lightroom 插件"
    changed, boundary = _apply_job_progress(
        job,
        {
            "event": "progress",
            "phase": "launch",
            "label": "等待 Lightroom 插件",
            "current": 24,
            "total": 120,
            "unit": "秒",
            "cached": 0,
        },
    )
    assert changed is False and boundary is False
    assert job["stage"] == "等待 Lightroom 插件"
    assert job["message"] == "24 / 120秒"
    assert job["progress"]["stage_percent"] == 20.0


def test_style_preset_probe_cannot_complete_real_preview_node_early() -> None:
    plan = _job_progress_plan("style_recommend", {"scope": "groups"})
    job = {
        "kind": "style_recommend",
        "context": {"scope": "groups"},
        "progress": _initial_job_progress(plan),
    }

    _apply_job_progress(
        job,
        {
            "event": "phase_end",
            "phase": "presets",
            "label": "核对 Lightroom 可用预设",
            "current": 1,
            "total": 1,
            "unit": "项",
            "cached": 0,
        },
    )

    assert job["progress"]["overall_percent"] == 10.0
    statuses = {node["key"]: node["status"] for node in job["progress"]["nodes"]}
    assert statuses["presets"] == "completed"
    assert statuses["process"] == "pending"

    for phase in ("representative", "scene", "recall"):
        _apply_job_progress(
            job,
            {
                "event": "phase_end",
                "phase": phase,
                "label": phase,
                "current": 25,
                "total": 25,
                "unit": "组",
                "cached": 0,
            },
        )
    before_render = job["progress"]["overall_percent"]
    _apply_job_progress(
        job,
        {
            "event": "progress",
            "phase": "process",
            "label": "Lightroom 真实预览",
            "current": 9,
            "total": 93,
            "unit": "项预览",
            "cached": 0,
        },
    )

    assert before_render == 40.0
    assert job["progress"]["overall_percent"] == 43.4
    assert job["progress"]["stage_percent"] == 9.7
    assert job["progress"]["current"] == 9
    assert job["progress"]["total"] == 93
    assert job["progress"]["stage_label"] == "Lightroom 真实预览"

    _apply_job_progress(
        job,
        {
            "event": "phase_end",
            "phase": "process",
            "label": "Lightroom 真实预览",
            "current": 93,
            "total": 93,
            "unit": "项预览",
            "cached": 0,
        },
    )
    render_complete = job["progress"]["overall_percent"]
    _apply_job_progress(
        job,
        {
            "event": "progress",
            "phase": "collect",
            "label": "校验 Lightroom 预览",
            "current": 9,
            "total": 93,
            "unit": "项",
            "cached": 0,
        },
    )

    assert render_complete == 75.0
    assert job["progress"]["overall_percent"] == 75.5
    assert job["progress"]["stage_percent"] == 9.7
    assert job["progress"]["stage_key"] == "collect"
    assert job["progress"]["stage_label"] == "校验 Lightroom 预览"


def test_stale_style_job_reports_disconnected_heartbeat_immediately(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class IdleThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr("landscape_culler.web.threading.Thread", IdleThread)
    manager = JobManager(tmp_path / "data", tmp_path)

    job = manager.start(
        "style_recommend",
        ["style-recommend"],
        {
            "title": "AI 风格推荐",
            "lightroom_state": "stale",
            "requires_lightroom": True,
        },
    )

    assert job["stage"] == "等待 Lightroom 插件"
    assert job["progress"]["stage_key"] == "launch"
    assert job["message"] == "Lightroom 插件心跳已断开，正在等待重新连接。"


def test_model_install_launch_never_mentions_lightroom(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class IdleThread:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def start(self) -> None:
            pass

    monkeypatch.setattr("landscape_culler.web.threading.Thread", IdleThread)
    manager = JobManager(tmp_path / "data", tmp_path)

    job = manager.start(
        "model_download",
        ["model-resources-configure", "--profile", "8gb"],
        {
            "title": "配置 8GB 显存 AI 模型",
            "profile_id": "8gb",
            "managed_engine": True,
        },
    )

    assert job["stage"] == "启动安装程序"
    assert job["message"] == "已加入本机任务队列。"
    assert "Lightroom" not in job["message"]


def test_feature_cache_hits_still_emit_complete_phases(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PHOTO_AI_PROGRESS", "json")
    paths = [tmp_path / "one.ARW", tmp_path / "two.ARW"]
    extractor = FeatureExtractor(tmp_path / "cache", use_dino=False)
    for index, path in enumerate(paths):
        path.write_bytes(b"raw")
        cached = extractor._cached_path(path)
        cached.parent.mkdir(parents=True, exist_ok=True)
        np.save(cached, np.full(8, index, dtype=np.float32))

    matrix, extracted = extractor.extract(paths)
    events = _events(capsys.readouterr().out)
    feature_end = next(
        event
        for event in events
        if event["phase"] == "features" and event["event"] == "phase_end"
    )
    assert matrix.shape == (2, 8)
    assert extracted == paths
    assert feature_end["current"] == feature_end["total"] == 2
    assert feature_end["cached"] == 2


def test_aesthetic_cache_hits_still_emit_complete_phase(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PHOTO_AI_PROGRESS", "json")
    scorer = GeneralAestheticScorer(tmp_path / "data")
    cached = {
        "model": "qrealign",
        "pipeline_version": "test",
        "preview_version": "test",
        "quality": 0.7,
        "aesthetic": 0.8,
    }
    monkeypatch.setattr(scorer, "_cached", lambda _path: cached)

    result = scorer.score([tmp_path / "one.ARW", tmp_path / "two.ARW"])
    events = _events(capsys.readouterr().out)
    end = next(event for event in events if event["event"] == "phase_end")
    assert len(result) == 2
    assert end["phase"] == "aesthetic"
    assert end["current"] == end["total"] == end["cached"] == 2


def test_group_critic_reports_photo_coverage(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("PHOTO_AI_PROGRESS", "json")
    paths = [tmp_path / "one.jpg", tmp_path / "two.jpg"]
    critic = OllamaGroupCritic(tmp_path / "data")
    monkeypatch.setattr(critic, "ensure_ready", lambda: None)

    def fake_critique(chunk: list[Path], context: str = "group") -> list[dict]:
        return [
            {
                "id": "IMG_01",
                "composition": 70,
                "light": 70,
                "subject_layers": 70,
                "color": 70,
                "technical_quality": 70,
                "edit_potential": 70,
                "distraction": 20,
                "rank": 1,
                "confidence": 0.8,
                "strengths": [],
                "issues": [],
                "summary": path.name,
            }
            for path in chunk
        ]

    monkeypatch.setattr(critic, "critique", fake_critique)
    output = critic.critique_groups(paths, [[0], [1]])
    events = _events(capsys.readouterr().out)
    progress = [event["current"] for event in events if event["event"] == "progress"]
    assert set(output) == {0, 1}
    assert progress == [1, 2]
    assert events[-1]["event"] == "phase_end"
