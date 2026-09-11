from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from landscape_culler import cli


def _style_args(run_dir: Path, data_dir: Path) -> list[str]:
    return [
        "landscape-culler",
        "style-recommend",
        "--run-dir",
        str(run_dir),
        "--data-dir",
        str(data_dir),
        "--batch-id",
        "style-cli-test",
        "--base-revision",
        "3",
    ]


def test_cli_no_longer_exposes_personal_training_commands() -> None:
    parser = cli.build_parser()
    help_text = parser.format_help()

    assert "train" not in help_text
    assert "audit" not in help_text
    with pytest.raises(SystemExit) as train_error:
        parser.parse_args(["train"])
    with pytest.raises(SystemExit) as audit_error:
        parser.parse_args(["audit"])
    assert train_error.value.code == 2
    assert audit_error.value.code == 2


def test_xmp_cleanup_cli_dispatches_execute_and_rollback(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    plan = tmp_path / "plan.json"
    manifest = tmp_path / "manifest.json"
    transactions = tmp_path / "transactions"
    plan.write_text("{}", encoding="utf-8")
    manifest.write_text("{}", encoding="utf-8")
    calls: list[tuple[str, Path, Path | None]] = []
    monkeypatch.setattr(
        cli,
        "execute_xmp_cleanup_plan",
        lambda plan_path, transactions_dir: (
            calls.append(("execute", plan_path, transactions_dir)) or {"status": "complete"}
        ),
    )
    monkeypatch.setattr(
        cli,
        "rollback_xmp_cleanup_manifest",
        lambda manifest_path: (
            calls.append(("rollback", manifest_path, None)) or {"status": "rolled_back"}
        ),
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "landscape-culler",
            "xmp-cleanup-execute",
            "--plan",
            str(plan),
            "--transactions-dir",
            str(transactions),
        ],
    )
    cli.main()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "landscape-culler",
            "xmp-cleanup-rollback",
            "--manifest",
            str(manifest),
        ],
    )
    cli.main()

    assert calls == [
        ("execute", plan, transactions),
        ("rollback", manifest, None),
    ]
    assert '"status": "complete"' in capsys.readouterr().out


def test_develop_plan_cli_dispatches_only_after_parsing_existing_paths(
    tmp_path: Path, monkeypatch
) -> None:
    input_path = tmp_path / "develop-input.json"
    run_dir = tmp_path / "run"
    input_path.write_text("{}", encoding="utf-8")
    run_dir.mkdir()
    captured: dict[str, Any] = {}

    def fake_worker(input_value: Path, run_value: Path, revision: int) -> dict[str, str]:
        captured.update(input=input_value, run_dir=run_value, revision=revision)
        return {"status": "complete"}

    monkeypatch.setattr(cli, "run_develop_plan_job", fake_worker)
    result = cli.run(
        [
            "develop-plan",
            "--input",
            str(input_path),
            "--run-dir",
            str(run_dir),
            "--review-revision",
            "7",
        ]
    )

    assert result == {"status": "complete"}
    assert captured == {"input": input_path, "run_dir": run_dir, "revision": 7}


@pytest.mark.parametrize(
    "extra",
    [
        ["--preset-id", "uuid:p0"],
        ["--preset-hash", "hash-0"],
        ["--preview-only", "--preset-id", "uuid:p0"],
        ["--preview-only", "--preset-hash", "hash-0"],
        ["--preview-only"],
    ],
)
def test_style_cli_rejects_unpaired_preset_identity(
    tmp_path: Path,
    monkeypatch,
    extra: list[str],
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(sys, "argv", [*_style_args(run_dir, tmp_path / "data"), *extra])
    monkeypatch.setattr(
        cli,
        "run_style_worker",
        lambda *_args, **_kwargs: pytest.fail("invalid CLI reached worker"),
    )

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == 2


def test_style_cli_passes_exact_preset_identity_to_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            *_style_args(run_dir, tmp_path / "data"),
            "--group-id",
            "1",
            "--preset-id",
            "uuid:p0",
            "--preset-hash",
            "hash-0",
            "--amount",
            "65",
            "--preview-only",
        ],
    )
    captured: dict[str, Any] = {}

    def fake_worker(*args: Any, **kwargs: Any) -> dict[str, str]:
        captured.update(args=args, kwargs=kwargs)
        return {"status": "complete"}

    monkeypatch.setattr(cli, "run_style_worker", fake_worker)

    cli.main()

    assert captured["kwargs"]["preset_id"] == "uuid:p0"
    assert captured["kwargs"]["preset_hash"] == "hash-0"
    assert captured["kwargs"]["amount"] == 65
    assert captured["kwargs"]["preview_only"] is True


def test_style_cli_all_groups_dispatches_explicit_group_scope(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [*_style_args(run_dir, tmp_path / "data"), "--all-groups"],
    )
    captured: dict[str, Any] = {}

    def fake_worker(*args: Any, **kwargs: Any) -> dict[str, str]:
        captured.update(args=args, kwargs=kwargs)
        return {"status": "complete"}

    monkeypatch.setattr(cli, "run_style_worker", fake_worker)

    cli.main()

    assert captured["kwargs"]["scope"] == "group"
    assert captured["kwargs"]["group_id"] is None
    assert captured["kwargs"]["preserve_confirmed"] is True


@pytest.mark.parametrize(
    "extra",
    [
        ["--all-groups", "--group-id", "1"],
        [
            "--all-groups",
            "--preset-id",
            "uuid:p0",
            "--preset-hash",
            "hash-0",
        ],
    ],
)
def test_style_cli_all_groups_rejects_conflicting_target_or_resource(
    tmp_path: Path,
    monkeypatch,
    extra: list[str],
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(sys, "argv", [*_style_args(run_dir, tmp_path / "data"), *extra])
    monkeypatch.setattr(
        cli,
        "run_style_worker",
        lambda *_args, **_kwargs: pytest.fail("invalid CLI reached worker"),
    )

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == 2


@pytest.mark.parametrize(
    "extra",
    [
        ["--lut-id", "cube-1", "--preview-only"],
        ["--lut-hash", "lut-hash", "--preview-only"],
        ["--lut-id", "cube-1", "--lut-hash", "lut-hash"],
        [
            "--preset-id",
            "uuid:p0",
            "--preset-hash",
            "hash-0",
            "--lut-id",
            "cube-1",
            "--lut-hash",
            "lut-hash",
            "--preview-only",
        ],
    ],
)
def test_style_cli_rejects_invalid_lut_identity(
    tmp_path: Path,
    monkeypatch,
    extra: list[str],
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(sys, "argv", [*_style_args(run_dir, tmp_path / "data"), *extra])
    monkeypatch.setattr(
        cli,
        "run_style_worker",
        lambda *_args, **_kwargs: pytest.fail("invalid CLI reached worker"),
    )

    with pytest.raises(SystemExit) as error:
        cli.main()

    assert error.value.code == 2


def test_style_cli_passes_exact_lut_identity_to_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            *_style_args(run_dir, tmp_path / "data"),
            "--group-id",
            "1",
            "--lut-id",
            "cube-1",
            "--lut-hash",
            "lut-hash",
            "--amount",
            "150",
            "--preview-only",
        ],
    )
    captured: dict[str, Any] = {}

    def fake_worker(*args: Any, **kwargs: Any) -> dict[str, str]:
        captured.update(args=args, kwargs=kwargs)
        return {"status": "complete"}

    monkeypatch.setattr(cli, "run_style_worker", fake_worker)

    cli.main()

    assert captured["kwargs"]["lut_id"] == "cube-1"
    assert captured["kwargs"]["lut_hash"] == "lut-hash"
    assert captured["kwargs"]["amount"] == 150
    assert captured["kwargs"]["preview_only"] is True
