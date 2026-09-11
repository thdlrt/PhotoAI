from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_model_install_page_keeps_observable_download_and_install_progress() -> None:
    template = (ROOT / "src/landscape_culler/templates/index.html").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "src/landscape_culler/static/app.js").read_text(encoding="utf-8")

    assert 'id="model-install-progress"' not in template
    assert 'id="model-resource-list"' in template
    assert 'id="settings-content-root"' in template

    for field in (
        "current_resource",
        "downloaded_bytes",
        "total_bytes",
        "bytes_per_second",
        "eta_seconds",
        "resumed_bytes",
    ):
        assert field in script
    assert "data-model-install-retry" in script
    assert "retryFailedJob" in script
    assert '["queued", "running", "cancelling", "failed", "interrupted"]' in script
    assert 'progress.unit === "B" ? current : null' in script
    assert "data-model-install-target" in script
    assert "data-model-install-cancel" in script
    assert 'data-model-offline-import="16gb"' in script
    assert 'invoke("pick_offline_bundle")' in script
    assert '"/api/model-resources/import-offline"' in script


def test_settings_transfer_stays_compact() -> None:
    template = (ROOT / "src/landscape_culler/templates/index.html").read_text(
        encoding="utf-8"
    )
    css = (ROOT / "src/landscape_culler/static/app.css").read_text(encoding="utf-8")

    assert 'class="panel settings-transfer"' in template
    assert ".settings-transfer .settings-row { min-height: 30px; }" in css
    assert ".settings-transfer .settings-actions { flex: none;" in css


def test_model_delete_keeps_result_visible_and_shows_busy_label() -> None:
    script = (ROOT / "src/landscape_culler/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "src/landscape_culler/static/app.css").read_text(encoding="utf-8")

    assert 'deleteProfile.textContent = state.modelResourceBusy === `profile:${selected.id}` ? "正在删除…"' in script
    assert "state.modelResourceMessage = { kind: \"error\", text: error.message };" in script
    assert ".resource-independent-note.success" in css
    assert ".resource-independent-note.error" in css
