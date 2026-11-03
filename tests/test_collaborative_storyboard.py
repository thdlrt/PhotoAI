from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from landscape_culler.collaborative_review import ReviewConflict, ReviewStore
from landscape_culler.collaborative_review_api import create_review_router
from landscape_culler.collaborative_storyboard import StoryboardStore, storyboard_csv
from landscape_culler.collaborative_storyboard_jobs import StoryboardJobs, StoryboardModelClient


def fixture(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    for name in ("photo1.jpg", "photo2.jpg", "unreviewed.jpg"):
        Image.new("RGB", (30, 20), "red").save(source / name)
    for name in ("video1.mp4", "reject.mp4"):
        (source / name).write_bytes(b"synthetic-video")
    reviews = ReviewStore(tmp_path / "data", video_probe=lambda _: {"duration": 10.0})
    workspace = reviews.create(str(source))
    wid = workspace["workspace_id"]
    ids = {item["name"]: item["id"] for item in workspace["items"]}
    reviews.update_item(wid, ids["photo1.jpg"], 0, {"status": "keep", "chapter": "开场", "order": 0, "notes": "这是我的必用照片"})
    reviews.update_item(wid, ids["video1.mp4"], 0, {"status": "keep", "chapter": "开场", "order": 1,
                        "segments": [{"in": 1, "out": 3, "note": "转身"}, {"in": 7, "out": 9, "note": "离开"}]})
    reviews.update_item(wid, ids["photo2.jpg"], 0, {"status": "maybe", "chapter": "结尾", "order": 2})
    reviews.update_item(wid, ids["reject.mp4"], 0, {"status": "reject"})
    return reviews, StoryboardStore(reviews), reviews.get(wid), ids


def clean_chapters(proposal):
    return copy.deepcopy(proposal["chapters"])


def test_rule_draft_obeys_decisions_segments_chapters_and_order_without_changes(tmp_path):
    reviews, story, workspace, ids = fixture(tmp_path)
    wid = workspace["workspace_id"]
    before = reviews.path(wid).read_bytes()
    originals = {path: path.read_bytes() for path in Path(workspace["input_root"]).iterdir()}
    proposal = story.create_rules(wid, workspace["revision"])
    assert proposal["source"] == "rules" and proposal["status"] == "draft"
    assert proposal["revision"] == 0 and proposal["stale"] is False
    assert [chapter["title"] for chapter in proposal["chapters"]] == ["开场", "结尾"]
    clips = [clip for chapter in proposal["chapters"] for clip in chapter["clips"]]
    assert [clip["asset_id"] for clip in clips] == [ids["photo1.jpg"], ids["video1.mp4"], ids["video1.mp4"], ids["photo2.jpg"]]
    assert [(clip["source_in"], clip["source_out"]) for clip in clips[1:3]] == [(1, 3), (7, 9)]
    assert proposal["source_snapshot"][ids["photo1.jpg"]]["user"]["notes"] == "这是我的必用照片"
    assert reviews.path(wid).read_bytes() == before
    assert all(path.read_bytes() == content for path, content in originals.items())


def test_import_rejects_excluded_asset_and_omitted_required_asset(tmp_path):
    _, story, workspace, ids = fixture(tmp_path)
    wid, rev = workspace["workspace_id"], workspace["revision"]
    proposal = story.create_rules(wid, rev)
    chapters = clean_chapters(proposal)
    chapters[0]["clips"].append({"asset_id": ids["reject.mp4"]})
    with pytest.raises(ValueError, match="不用"):
        story.import_proposal(wid, rev, chapters)
    chapters = clean_chapters(proposal)
    chapters[0]["clips"] = chapters[0]["clips"][1:]
    with pytest.raises(ValueError, match="遗漏人工必用"):
        story.import_proposal(wid, rev, chapters)


def test_import_rejects_changed_or_missing_required_segments(tmp_path):
    _, story, workspace, _ = fixture(tmp_path)
    wid, rev = workspace["workspace_id"], workspace["revision"]
    proposal = story.create_rules(wid, rev)
    chapters = clean_chapters(proposal)
    chapters[0]["clips"][1]["source_in"] = 1.5
    with pytest.raises(ValueError, match="人工源区间"):
        story.import_proposal(wid, rev, chapters)
    chapters = clean_chapters(proposal)
    chapters[0]["clips"].pop(2)
    with pytest.raises(ValueError, match="遗漏必用素材的人工片段"):
        story.import_proposal(wid, rev, chapters)


def test_import_rejects_overwriting_manual_chapter_order_and_duplicate_clip(tmp_path):
    _, story, workspace, _ = fixture(tmp_path)
    wid, rev = workspace["workspace_id"], workspace["revision"]
    proposal = story.create_rules(wid, rev)
    chapters = clean_chapters(proposal)
    chapters[0]["title"] = "AI 重排的章名"
    with pytest.raises(ValueError, match="人工章节"):
        story.import_proposal(wid, rev, chapters)
    chapters = list(reversed(clean_chapters(proposal)))
    with pytest.raises(ValueError, match="人工顺序"):
        story.import_proposal(wid, rev, chapters)
    chapters = clean_chapters(proposal)
    chapters[0]["clips"].append(chapters[0]["clips"][0])
    with pytest.raises(ValueError, match="重复"):
        story.import_proposal(wid, rev, chapters)


def test_unreviewed_requires_opt_in_and_video_without_evidence_has_no_invented_cuts(tmp_path):
    reviews, story, workspace, ids = fixture(tmp_path)
    wid = workspace["workspace_id"]
    reviews.update_item(wid, ids["reject.mp4"], 1, {"status": "unreviewed"})
    workspace = reviews.get(wid)
    proposal = story.create_rules(wid, workspace["revision"], include_unreviewed=True)
    clip = next(c for chapter in proposal["chapters"] for c in chapter["clips"] if c["asset_id"] == ids["reject.mp4"])
    assert clip["source_in"] is None and clip["source_out"] is None
    assert any("源区间待标注" in warning for warning in proposal["warnings"])
    with pytest.raises(ValueError, match="未审素材"):
        story.import_proposal(wid, workspace["revision"], proposal["chapters"])
    chapters = clean_chapters(proposal)
    for chapter in chapters:
        for c in chapter["clips"]:
            if c["asset_id"] == ids["reject.mp4"]:
                c.update(source_in=0, source_out=5)
    with pytest.raises(ValueError, match="不能编造"):
        story.import_proposal(wid, workspace["revision"], chapters, source="configured-model", include_unreviewed=True)
    # An external agent may have independently reviewed the video and supply
    # explicit, validated new intervals; provenance remains external-agent.
    external = story.import_proposal(wid, workspace["revision"], chapters, include_unreviewed=True,
                                      evidence="外部代理独立审看片段；待用户确认。")
    assert external["source"] == "external-agent"


@pytest.mark.parametrize("source_in,source_out", [(0, 11), (9, 2), (-1, 2), (float("nan"), 2), (0, None)])
def test_external_video_intervals_must_fit_actual_source(tmp_path, source_in, source_out):
    reviews, story, workspace, ids = fixture(tmp_path)
    wid = workspace["workspace_id"]
    reviews.update_item(wid, ids["video1.mp4"], 1, {"segments": []})
    workspace = reviews.get(wid)
    proposal = story.create_rules(wid, workspace["revision"])
    chapters = clean_chapters(proposal)
    chapters[0]["clips"][1].update(source_in=source_in, source_out=source_out)
    with pytest.raises(ValueError):
        story.import_proposal(wid, workspace["revision"], chapters)


def test_revisioned_approval_never_changes_reviews_and_stale_approval_is_rejected(tmp_path):
    reviews, story, workspace, ids = fixture(tmp_path)
    wid, wrev = workspace["workspace_id"], workspace["revision"]
    before = reviews.path(wid).read_bytes()
    proposal = story.create_rules(wid, wrev)
    approved = story.update(wid, proposal["proposal_id"], 0, wrev, {"status": "approved"})
    assert approved["status"] == "approved" and approved["approval_valid"] is True
    assert reviews.path(wid).read_bytes() == before
    assert story.get(wid, proposal["proposal_id"], revision=0)["status"] == "draft"
    assert [row["revision"] for row in story.history(wid, proposal["proposal_id"])] == [1, 0]
    with pytest.raises(ReviewConflict):
        story.update(wid, proposal["proposal_id"], 0, wrev, {"title": "stale write"})
    changed = reviews.update_item(wid, ids["photo1.jpg"], 1, {"notes": "最新要求"})
    stale = story.get(wid, proposal["proposal_id"])
    assert stale["stale"] is True and stale["approval_valid"] is False
    with pytest.raises(ReviewConflict, match="已变化"):
        story.update(wid, proposal["proposal_id"], 1, changed["revision"], {"status": "approved"})
    rebased = story.update(wid, proposal["proposal_id"], 1, changed["revision"], {"title": "复核后版本"})
    assert rebased["status"] == "draft" and rebased["stale"] is False
    approved = story.update(wid, proposal["proposal_id"], 2, changed["revision"], {"status": "approved"})
    assert approved["approval_valid"] is True


def test_export_records_source_time_user_notes_and_draft_status(tmp_path):
    _, story, workspace, _ = fixture(tmp_path)
    wid, rev = workspace["workspace_id"], workspace["revision"]
    proposal = story.create_rules(wid, rev, title="测试草案")
    proposal = story.update(wid, proposal["proposal_id"], 0, rev, {"brief": "=literal"})
    exported = story.export(wid, proposal["proposal_id"])
    assert exported["time_basis"] == "original-source-seconds"
    assert exported["status"] == "draft" and exported["approval_valid"] is False
    assert exported["clips"][0]["user_notes"] == "这是我的必用照片"
    assert exported["clips"][1]["source_in"] == 1
    assert "proposal_status" in storyboard_csv(exported)


def test_historical_approval_is_auditable_but_only_latest_revision_is_actionable(tmp_path):
    _, story, workspace, _ = fixture(tmp_path)
    wid, rev = workspace["workspace_id"], workspace["revision"]
    proposal = story.create_rules(wid, rev, title="原始草案")
    pid = proposal["proposal_id"]
    approved = story.update(wid, pid, 0, rev, {"status": "approved"})
    assert approved["approval_valid"] is True and approved["is_latest"] is True
    changed = story.update(wid, pid, 1, rev, {"title": "后续新草稿"})
    assert changed["status"] == "draft"
    historical = story.get(wid, pid, revision=1)
    assert historical["status"] == "approved"
    assert historical["is_latest"] is False and historical["latest_revision"] == 2
    assert historical["approval_valid"] is False
    exported = story.export(wid, pid, revision=1)
    assert exported["revision"] == 1 and exported["title"] == "原始草案"
    assert story.export(wid, pid)["title"] == "后续新草稿"


def test_context_is_text_only_no_absolute_paths_and_excludes_rejected(tmp_path):
    _, story, workspace, ids = fixture(tmp_path)
    context = story.context(workspace["workspace_id"], workspace["revision"])
    serialized = json.dumps(context, ensure_ascii=False)
    assert workspace["input_root"] not in serialized
    assert "base64" not in serialized and "images" not in context
    assert ids["reject.mp4"] in context["excluded_asset_ids"]
    assert ids["reject.mp4"] not in {item["asset_id"] for item in context["items"]}


def wait_job(jobs, wid, jid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = jobs.get(wid, jid)
        if job["status"] not in {"queued", "running", "cancelling"}:
            return job
        time.sleep(0.01)
    pytest.fail("model planning job did not finish")


def test_model_proposal_is_separate_and_tracks_actual_model_and_text_evidence(tmp_path):
    reviews, story, workspace, _ = fixture(tmp_path)
    wid, rev = workspace["workspace_id"], workspace["revision"]
    initial = story.create_rules(wid, rev)
    before = reviews.path(wid).read_bytes()
    seen = []
    class Provider:
        model = "mock-semantic-planner"
        def __call__(self, **kwargs):
            seen.append(kwargs)
            return {"chapters": initial["chapters"]}
    jobs = StoryboardJobs(story, provider_factory=lambda _: Provider())
    try:
        started = jobs.start(wid, rev, brief="按故事组织")
        done = wait_job(jobs, wid, started["job_id"])
        assert done["status"] == "done", done
        proposal = story.get(wid, done["proposal_id"])
        assert proposal["source"] == "configured-model" and proposal["model"] == "mock-semantic-planner"
        assert "未重新观看" in proposal["evidence"]
        assert seen[0]["brief"] == "按故事组织"
        assert reviews.path(wid).read_bytes() == before
    finally:
        jobs.shutdown()


@pytest.mark.parametrize("cancel", [False, True])
def test_inflight_model_rejects_concurrent_human_changes_or_cancel(tmp_path, cancel):
    reviews, story, workspace, ids = fixture(tmp_path)
    wid, rev = workspace["workspace_id"], workspace["revision"]
    chapters = story.create_rules(wid, rev)["chapters"]
    before_count = len(story.list(wid))
    entered, release = threading.Event(), threading.Event()
    class Provider:
        model = "mock"
        def __call__(self, **_):
            entered.set()
            assert release.wait(3)
            return {"chapters": chapters}
    jobs = StoryboardJobs(story, provider_factory=lambda _: Provider())
    try:
        job = jobs.start(wid, rev)
        assert entered.wait(3)
        if cancel:
            jobs.cancel(wid, job["job_id"])
        else:
            reviews.update_item(wid, ids["photo1.jpg"], 1, {"status": "reject"})
        release.set()
        done = wait_job(jobs, wid, job["job_id"])
        assert done["status"] == ("cancelled" if cancel else "failed")
        assert done["proposal_id"] is None and len(story.list(wid)) == before_count
    finally:
        release.set()
        jobs.shutdown()


def test_model_transport_never_adds_images_to_semantic_request(tmp_path):
    _, story, workspace, _ = fixture(tmp_path)
    context = story.context(workspace["workspace_id"], workspace["revision"])
    client = object.__new__(StoryboardModelClient)
    client.model = "mock-model"
    requests = []
    class Transport:
        def request(self, payload):
            requests.append(payload)
            return {"message": {"content": '{"chapters":[]}'}}
    client.transport = Transport()
    client(context=context, title="计划", brief="舒缓叙事")
    assert "images" not in requests[0]["messages"][0]
    assert "没有图片、视频或声音输入" in requests[0]["messages"][0]["content"]


def test_storyboard_api_create_import_update_history_export_and_tokens(tmp_path):
    reviews, _, workspace, _ = fixture(tmp_path)
    def token(x_photo_ai_token: str | None = Header(None)):
        if x_photo_ai_token != "ok":
            raise HTTPException(403)
    router = create_review_router(reviews.data_dir, token, store=reviews)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    wid, rev = workspace["workspace_id"], workspace["revision"]
    url = f"/api/review-workspaces/{wid}/storyboards"
    try:
        assert client.post(url, json={"workspace_revision": rev}).status_code == 403
        response = client.post(url, headers={"X-Photo-AI-Token": "ok"}, json={"workspace_revision": rev, "mode": "rules"})
        assert response.status_code == 200, response.text
        proposal = response.json()["proposal"]
        pid = proposal["proposal_id"]
        assert len(client.get(url).json()["proposals"]) == 1
        approved = client.patch(url + "/" + pid, headers={"X-Photo-AI-Token": "ok"},
                                json={"revision": 0, "workspace_revision": rev, "status": "approved"})
        assert approved.status_code == 200 and approved.json()["proposal"]["approval_valid"] is True
        assert len(client.get(url + "/" + pid + "/history").json()["revisions"]) == 2
        assert client.get(url + "/" + pid + "?revision=0").json()["proposal"]["status"] == "draft"
        exported = client.get(url + "/" + pid + "/export?format=csv")
        assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
        assert "source_in" in exported.text
        historical_export = client.get(url + "/" + pid + "/export?format=json&revision=0").json()
        assert historical_export["revision"] == 0 and historical_export["status"] == "draft"
        assert historical_export["is_latest"] is False
        imported = client.post(url, headers={"X-Photo-AI-Token": "ok"}, json={"workspace_revision": rev,
                               "mode": "import", "chapters": proposal["chapters"], "evidence": "Codex独立编排"})
        assert imported.status_code == 200 and imported.json()["proposal"]["source"] == "external-agent"
    finally:
        router.review_jobs.shutdown()
