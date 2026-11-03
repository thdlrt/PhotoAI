"""FastAPI adapter for the independent collaborative-review store."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .collaborative_review import ReviewConflict, ReviewCorrupt, ReviewStore, export_csv
from .collaborative_review_vision import ReviewVisionJobs, preview_image
from .collaborative_storyboard import storyboard_csv


class CreateReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_root: str = Field(min_length=1, max_length=4000)
    name: str | None = Field(default=None, max_length=200)


class ReviewRevisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)


class ReviewItemBody(ReviewRevisionBody):
    status: str | None = None
    quality: int | None = Field(default=None, ge=0, le=3, strict=True)
    beauty: int | None = Field(default=None, ge=0, le=3, strict=True)
    memory: int | None = Field(default=None, ge=0, le=3, strict=True)
    chapter: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=10000)
    issues: list[str] | None = None
    segments: list[dict[str, Any]] | None = None
    rotation: int | None = Field(default=None, strict=True)
    order: int | None = Field(default=None, ge=0, le=1000000, strict=True)


class ReviewSuggestBody(ReviewRevisionBody):
    mode: Literal["organize", "existing_ai", "vision"] = "organize"
    asset_ids: list[str] | None = None
    limit: int = Field(default=12, ge=1, le=24, strict=True)


class ImportSuggestionsBody(ReviewRevisionBody):
    items: dict[str, dict[str, Any]]


class ReviewProxyBody(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    start: float = Field(default=0, ge=0, strict=True)
    duration: float = Field(default=60, gt=0, le=60, strict=True)


class CreateStoryboardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_revision: int = Field(ge=0, strict=True)
    mode: Literal["rules", "model", "import"] = "rules"
    title: str = Field(default="", max_length=300)
    brief: str = Field(default="", max_length=10000)
    include_unreviewed: bool = False
    chapters: list[dict[str, Any]] | None = None
    evidence: str = Field(default="", max_length=4000)


class UpdateStoryboardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0, strict=True)
    workspace_revision: int = Field(ge=0, strict=True)
    title: str | None = Field(default=None, max_length=300)
    brief: str | None = Field(default=None, max_length=10000)
    chapters: list[dict[str, Any]] | None = None
    status: Literal["draft", "approved", "rejected"] | None = None


def _call(operation: Callable):
    try:
        return operation()
    except (ReviewConflict, ReviewCorrupt) as exc:
        raise HTTPException(409, str(exc)) from None
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from None
    except (ValueError, TypeError, OSError) as exc:
        raise HTTPException(422, str(exc)) from None


def create_review_router(data_dir: Path, mutate_token: Callable, *, store: ReviewStore | None = None,
                         vision_jobs: ReviewVisionJobs | None = None) -> APIRouter:
    store = store or ReviewStore(data_dir)
    jobs = vision_jobs or ReviewVisionJobs(store)
    router = APIRouter(prefix="/api/review-workspaces", tags=["collaborative-review"])
    # The host's lifespan should call router.review_jobs.shutdown().
    router.review_jobs = jobs
    router.review_store = store
    mutation = [Depends(mutate_token)]

    @router.get("")
    def list_workspaces():
        return {"workspaces": _call(store.list)}

    @router.post("", dependencies=mutation)
    def create_workspace(body: CreateReviewBody):
        return _call(lambda: store.create(body.input_root, body.name))

    @router.get("/{workspace_id}")
    def get_workspace(workspace_id: str):
        return _call(lambda: store.get(workspace_id))

    @router.post("/{workspace_id}/rescan", dependencies=mutation)
    def rescan_workspace(workspace_id: str, body: ReviewRevisionBody):
        return _call(lambda: store.rescan(workspace_id, body.revision))

    @router.patch("/{workspace_id}/items/{asset_id}", dependencies=mutation)
    def update_item(workspace_id: str, asset_id: str, body: ReviewItemBody):
        changes = body.model_dump(exclude_unset=True, exclude={"revision"})
        return _call(lambda: store.update_item(workspace_id, asset_id, body.revision, changes))

    @router.post("/{workspace_id}/suggest", dependencies=mutation)
    def suggest(workspace_id: str, body: ReviewSuggestBody):
        if body.mode == "vision":
            return {"job": _call(lambda: jobs.start(workspace_id, body.revision, body.asset_ids, body.limit))}
        return _call(lambda: store.organize(workspace_id, body.revision, existing_ai=body.mode == "existing_ai"))

    @router.post("/{workspace_id}/suggestions", dependencies=mutation)
    def import_suggestions(workspace_id: str, body: ImportSuggestionsBody):
        return _call(lambda: store.suggestions(workspace_id, body.revision, body.items))

    @router.get("/{workspace_id}/jobs/latest")
    def latest_job(workspace_id: str):
        _call(lambda: store.get(workspace_id))
        return {"job": jobs.latest(workspace_id)}

    @router.get("/{workspace_id}/jobs/{job_id}")
    def get_job(workspace_id: str, job_id: str):
        return {"job": _call(lambda: jobs.get(workspace_id, job_id))}

    @router.post("/{workspace_id}/jobs/{job_id}/cancel", dependencies=mutation)
    def cancel_job(workspace_id: str, job_id: str):
        return {"job": _call(lambda: jobs.cancel(workspace_id, job_id))}

    @router.get("/{workspace_id}/media/{asset_id}")
    def media(workspace_id: str, asset_id: str):
        path = _call(lambda: store.media_path(workspace_id, asset_id))
        return FileResponse(path, headers={"Cache-Control": "private, no-cache"})

    @router.post("/{workspace_id}/proxy/{asset_id}", dependencies=mutation)
    def create_proxy(workspace_id: str, asset_id: str, body: ReviewProxyBody):
        return {"job": _call(lambda: jobs.proxy_jobs.start(workspace_id, asset_id, body.start, body.duration))}

    @router.get("/{workspace_id}/proxy-jobs/latest")
    def latest_proxy(workspace_id: str):
        _call(lambda: store.get(workspace_id))
        return {"job": jobs.proxy_jobs.latest(workspace_id)}

    @router.get("/{workspace_id}/proxy-jobs/{job_id}")
    def get_proxy(workspace_id: str, job_id: str):
        return {"job": _call(lambda: jobs.proxy_jobs.get(workspace_id, job_id))}

    @router.post("/{workspace_id}/proxy-jobs/{job_id}/cancel", dependencies=mutation)
    def cancel_proxy(workspace_id: str, job_id: str):
        return {"job": _call(lambda: jobs.proxy_jobs.cancel(workspace_id, job_id))}

    @router.get("/{workspace_id}/proxies/{job_id}")
    def proxy_media(workspace_id: str, job_id: str):
        path = _call(lambda: jobs.proxy_jobs.media_path(workspace_id, job_id))
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-cache"})

    @router.get("/{workspace_id}/preview/{asset_id}")
    def preview(workspace_id: str, asset_id: str):
        path = _call(lambda: preview_image(store, workspace_id, asset_id))
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @router.get("/{workspace_id}/storyboard-context")
    def storyboard_context(workspace_id: str, include_unreviewed: bool = False):
        workspace = _call(lambda: store.get(workspace_id))
        return {"context": _call(lambda: jobs.storyboards.store.context(workspace_id, workspace["revision"],
                                                                        include_unreviewed=include_unreviewed))}

    @router.get("/{workspace_id}/storyboards")
    def storyboards(workspace_id: str):
        return {"proposals": _call(lambda: jobs.storyboards.store.list(workspace_id))}

    @router.post("/{workspace_id}/storyboards", dependencies=mutation)
    def create_storyboard(workspace_id: str, body: CreateStoryboardBody):
        options = {"title": body.title, "brief": body.brief, "include_unreviewed": body.include_unreviewed}
        if body.mode == "model":
            return {"job": _call(lambda: jobs.storyboards.start(workspace_id, body.workspace_revision, **options))}
        if body.mode == "rules":
            return {"proposal": _call(lambda: jobs.storyboards.store.create_rules(workspace_id, body.workspace_revision, **options))}
        if body.chapters is None:
            raise HTTPException(422, "导入草案必须包含 chapters。")
        return {"proposal": _call(lambda: jobs.storyboards.store.import_proposal(workspace_id, body.workspace_revision,
                                                                                body.chapters, evidence=body.evidence, **options))}

    @router.get("/{workspace_id}/storyboards/{proposal_id}")
    def get_storyboard(workspace_id: str, proposal_id: str, revision: int | None = Query(default=None, ge=0)):
        return {"proposal": _call(lambda: jobs.storyboards.store.get(workspace_id, proposal_id, revision))}

    @router.patch("/{workspace_id}/storyboards/{proposal_id}", dependencies=mutation)
    def update_storyboard(workspace_id: str, proposal_id: str, body: UpdateStoryboardBody):
        changes = body.model_dump(exclude_unset=True, exclude={"revision", "workspace_revision"})
        return {"proposal": _call(lambda: jobs.storyboards.store.update(workspace_id, proposal_id, body.revision,
                                                                        body.workspace_revision, changes))}

    @router.get("/{workspace_id}/storyboards/{proposal_id}/history")
    def storyboard_history(workspace_id: str, proposal_id: str):
        return {"revisions": _call(lambda: jobs.storyboards.store.history(workspace_id, proposal_id))}

    @router.get("/{workspace_id}/storyboards/{proposal_id}/export")
    def export_storyboard(workspace_id: str, proposal_id: str, format: Literal["json", "csv"] = "json",
                          revision: int | None = Query(default=None, ge=0)):
        payload = _call(lambda: jobs.storyboards.store.export(workspace_id, proposal_id, revision=revision))
        body = storyboard_csv(payload) if format == "csv" else json.dumps(payload, ensure_ascii=False, indent=2)
        return Response(body, media_type="text/csv; charset=utf-8" if format == "csv" else "application/json",
                        headers={"Content-Disposition": f'attachment; filename="storyboard-{proposal_id}.{format}"'})

    @router.get("/{workspace_id}/storyboard-jobs/latest")
    def latest_storyboard_job(workspace_id: str):
        _call(lambda: store.get(workspace_id))
        return {"job": jobs.storyboards.latest(workspace_id)}

    @router.get("/{workspace_id}/storyboard-jobs/{job_id}")
    def get_storyboard_job(workspace_id: str, job_id: str):
        return {"job": _call(lambda: jobs.storyboards.get(workspace_id, job_id))}

    @router.post("/{workspace_id}/storyboard-jobs/{job_id}/cancel", dependencies=mutation)
    def cancel_storyboard_job(workspace_id: str, job_id: str):
        return {"job": _call(lambda: jobs.storyboards.cancel(workspace_id, job_id))}

    @router.get("/{workspace_id}/export")
    def export(workspace_id: str, format: Literal["json", "csv"] = "json", selected_only: bool = True):
        payload = _call(lambda: store.export(workspace_id, selected_only=selected_only))
        body = export_csv(payload) if format == "csv" else json.dumps(payload, ensure_ascii=False, indent=2)
        return Response(body, media_type="text/csv; charset=utf-8" if format == "csv" else "application/json",
                        headers={"Content-Disposition": f'attachment; filename="review-{workspace_id}.{format}"'})

    return router
