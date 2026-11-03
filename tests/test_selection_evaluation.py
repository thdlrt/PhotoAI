import copy
import json
import math
import subprocess
import sys
from pathlib import Path

import pytest

from landscape_culler.selection_evaluation import create_reference_template, evaluate_selection


def fixture():
    payload = {"workflow_state": "scored", "input_root": "C:\\photos", "retain_ratio": .5,
               "results": [{"path": f"C:\\photos\\DSC000{i}.ARW", "source_key": f"source-{i}",
                            "score": score, "rating": 0} for i, score in enumerate([.9, .8, .7, .6])]}
    reference = create_reference_template(payload)
    reference.update(label_protocol="independent", rater="test-rater")
    for row, grade in zip(reference["photos"], [5, 1, 4, 0]):
        row.update(human_score=grade, technical_failure=grade == 1)
    return payload, reference


def test_template_contains_no_ai_or_effective_labels_and_requires_explicit_human_reference():
    payload, _ = fixture()
    payload["results"][0].update(effective_rating=5, manual_rating=5)
    template = create_reference_template(payload)
    assert template["label_protocol"] == "unlabeled"
    assert all(row["human_score"] is None for row in template["photos"])
    assert all("score" not in row and "rating" not in row for row in template["photos"])
    with pytest.raises(ValueError, match="人工标注尚未完成"):
        evaluate_selection(payload, template)


def test_fixed_budget_metrics_and_missed_better_photo_are_correct_and_read_only():
    payload, reference = fixture()
    before = copy.deepcopy((payload, reference))
    result = evaluate_selection(payload, reference)
    assert result["budget"] == 2
    assert result["metrics"]["selected_human_mean"] == 3
    assert result["metrics"]["ideal_human_mean"] == 4.5
    assert result["metrics"]["human_mean_regret"] == 1.5
    assert result["metrics"]["preferred_precision"] == .5
    assert result["metrics"]["preferred_recall"] == .5
    assert result["metrics"]["technical_failure_rate"] == .5
    assert result["metrics"]["ndcg_at_budget"] == pytest.approx((31 + 1 / math.log2(3)) / (31 + 15 / math.log2(3)))
    assert result["missed_above_selection_floor"][0]["photo_id"] == "dsc0002.arw"
    assert result["selected_technical_failures"] == ["dsc0001.arw"]
    assert (payload, reference) == before
    # AI/manual stars must not replace ranking when testing the fixed budget.
    assert [row["photo_id"] for row in result["selected"]] == ["dsc0000.arw", "dsc0001.arw"]
    payload["results"][1]["score"], payload["results"][2]["score"] = .7, .8
    improved = evaluate_selection(payload, reference)
    assert improved["metrics"]["human_mean_regret"] == 0
    assert improved["metrics"]["ndcg_at_budget"] == 1


def test_ratio_is_frozen_in_reference_and_ties_use_result_order():
    payload, reference = fixture()
    payload["retain_ratio"] = 1
    for row in payload["results"]: row["score"] = .7
    result = evaluate_selection(payload, reference)
    assert result["budget"] == 2
    assert result["selected"][0]["photo_id"] == "dsc0000.arw"
    assert evaluate_selection(payload, reference, .51)["budget"] == 3


def test_evaluation_uses_same_exact_decimal_budget_as_production():
    payload, _ = fixture()
    payload["results"] = [{"path": f"C:/photos/{index}.ARW", "source_key": str(index), "score": .5}
                          for index in range(100)]
    payload["retain_ratio"] = .07
    reference = create_reference_template(payload)
    reference.update(label_protocol="independent", rater="fixture")
    for row in reference["photos"]: row["human_score"] = 3
    assert evaluate_selection(payload, reference)["budget"] == 7


@pytest.mark.parametrize("corruption", ["duplicate", "key", "protocol", "rater", "technical", "schema"])
def test_reference_metadata_cannot_silently_change(corruption):
    payload, reference = fixture()
    if corruption == "duplicate": reference["photos"].append(reference["photos"][0])
    elif corruption == "key": reference["photos"][0]["source_key"] = "different"
    elif corruption == "protocol": reference["label_protocol"] = "ai-generated"
    elif corruption == "rater": reference["rater"] = ""
    elif corruption == "technical": reference["photos"][0]["technical_failure"] = "false"
    elif corruption == "schema": reference["schema_version"] = True
    with pytest.raises(ValueError): evaluate_selection(payload, reference)


@pytest.mark.parametrize("corruption", ["missing", "extra", "excluded", "changed", "duplicate", "outside", "traversal", "nan", "no_key"])
def test_mismatched_or_invalid_prediction_cannot_silently_improve_metrics(corruption):
    payload, reference = fixture()
    row = payload["results"][0]
    if corruption == "missing": payload["results"].pop()
    elif corruption == "extra": payload["results"].append({**row, "path": "C:/photos/new.ARW"})
    elif corruption == "excluded": row["excluded"] = True
    elif corruption == "changed": row["source_key"] = "different-source"
    elif corruption == "duplicate": payload["results"].append({**row, "path": row["path"].lower()})
    elif corruption == "outside": row["path"] = "C:/elsewhere/DSC0000.ARW"
    elif corruption == "traversal": row["path"] = "C:/photos/../DSC0000.ARW"
    elif corruption == "nan": row["score"] = float("nan")
    elif corruption == "no_key": row.pop("source_key")
    with pytest.raises(ValueError): evaluate_selection(payload, reference)


@pytest.mark.parametrize("grade", [None, True, -1, 6, 3.5, "4", float("nan")])
def test_all_photos_require_valid_human_labels(grade):
    payload, reference = fixture()
    reference["photos"][0]["human_score"] = grade
    with pytest.raises(ValueError, match="人工 0–5"):
        evaluate_selection(payload, reference)


def test_degenerate_labels_and_partial_technical_labels_are_explicit():
    payload, reference = fixture()
    reference["label_protocol"] = "assisted"
    for row in reference["photos"]: row.update(human_score=0, technical_failure=None)
    result = evaluate_selection(payload, reference)
    assert result["metrics"]["ndcg_at_budget"] is None
    assert result["metrics"]["preferred_recall"] is None
    assert result["metrics"]["technical_failure_rate"] is None
    assert result["metrics"]["technical_label_coverage"] == 0
    assert len(result["warnings"]) == 3
    json.dumps(result, allow_nan=False)


def test_excluded_and_deleted_photos_never_enter_reference_and_empty_is_rejected():
    payload, _ = fixture()
    payload["results"][0]["excluded"] = True
    payload["results"][1]["deleted"] = True
    assert len(create_reference_template(payload)["photos"]) == 2
    payload["results"] = []
    with pytest.raises(ValueError, match="没有可评测"):
        create_reference_template(payload)


def test_cli_reports_multiple_versions_without_rewriting_any_input(tmp_path):
    payload, reference = fixture()
    root = Path(__file__).parents[1]
    first, second, labels, report = [tmp_path / name for name in ["first.json", "second.json", "human.json", "report.json"]]
    first.write_text(json.dumps(payload), encoding="utf-8")
    payload["results"][1]["score"] = .1
    second.write_text(json.dumps(payload), encoding="utf-8")
    labels.write_text(json.dumps(reference), encoding="utf-8")
    before = {path: path.read_bytes() for path in [first, second, labels]}
    command = [sys.executable, str(root / "scripts/evaluate_selection.py"), "evaluate", "--reference", str(labels),
               "--results", str(first), str(second), "--output", str(report)]
    result = subprocess.run(command, capture_output=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(report.read_text(encoding="utf-8"))
    assert len(data["evaluations"]) == 2
    assert data["evaluations"][0]["metrics"]["human_mean_regret"] > data["evaluations"][1]["metrics"]["human_mean_regret"]
    # Even --output pointing at a source/reference must fail without truncation.
    assert subprocess.run(command[:-1] + [str(labels)], capture_output=True).returncode == 2
    assert all(path.read_bytes() == content for path, content in before.items())


def test_evaluation_module_does_not_load_models_or_access_photos():
    command = "import sys; import landscape_culler.selection_evaluation; assert not {'torch','numpy','PIL','rawpy'} & set(sys.modules)"
    result = subprocess.run([sys.executable, "-c", command], capture_output=True)
    assert result.returncode == 0, result.stderr
