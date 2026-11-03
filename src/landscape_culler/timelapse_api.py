"""HTTP surface for the local timelapse workspace."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .timelapse import TimelapseManager


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ScanRequest(_Request):
    input_root: str = Field(min_length=1, max_length=32768)
    extensions: list[str] | None = None


class ProcessRequest(_Request):
    base_revision: int | None = Field(default=None, ge=0)
    keyframe_mode: Literal["auto", "even", "existing", "manual"] = "auto"
    keyframe_count: int = Field(default=8, ge=2, le=1000)
    keyframe_indices: list[int] = Field(default_factory=list, max_length=20000)
    keyframe_exposures: dict[int, float] = Field(default_factory=dict)
    sensitivity: float = Field(default=1.0, ge=0.1, le=10)
    transition: bool = False
    holy_grail: bool = True
    deflicker: bool = True
    strength: float = Field(default=1.0, ge=0, le=1)
    smooth_window: int = Field(default=11, ge=3, le=501)

    @field_validator("keyframe_exposures")
    @classmethod
    def valid_exposures(cls, value):
        if any(not -5 <= exposure <= 5 for exposure in value.values()):
            raise ValueError("关键帧曝光必须在 -5 至 +5 EV 之间。")
        return value


class RenderRequest(_Request):
    base_revision: int | None = Field(default=None, ge=0)
    fps: float = Field(default=25, ge=1, le=120)
    width: int = Field(default=1920, ge=16, le=7680)
    height: int = Field(default=1080, ge=16, le=7680)
    crf: int = Field(default=18, ge=0, le=35)
    apply_corrections: bool = False
    allow_mixed_dimensions: bool = False

    @field_validator("width", "height")
    @classmethod
    def even_dimension(cls, value):
        if value % 2:
            raise ValueError("宽高需要为偶数。")
        return value


def create_timelapse_router(data_dir: Path, mutate_token: Callable | None = None) -> APIRouter:
    manager = TimelapseManager(data_dir)
    router = APIRouter(prefix="/api/toolbox/timelapse", tags=["timelapse"])
    router.timelapse_manager = manager
    mutation_dependencies = [Depends(mutate_token)] if mutate_token else []

    def call(function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/capabilities")
    def capabilities():
        return manager.capabilities()

    @router.get("/sessions")
    def sessions():
        return manager.list_sessions()

    @router.get("/sessions/{session_id}")
    def session(session_id: str):
        return call(manager.session, session_id)

    @router.post("/scan", dependencies=mutation_dependencies)
    def scan(body: ScanRequest):
        return call(manager.submit, "scan", body.model_dump())

    @router.post("/sessions/{session_id}/process", dependencies=mutation_dependencies)
    def process(session_id: str, body: ProcessRequest):
        return call(manager.submit, "process", body.model_dump(), session_id)

    @router.post("/sessions/{session_id}/render", dependencies=mutation_dependencies)
    def render(session_id: str, body: RenderRequest):
        return call(manager.submit, "render", body.model_dump(), session_id)

    @router.get("/sessions/{session_id}/frames/{index}")
    def frame(session_id: str, index: int):
        return Response(call(manager.preview, session_id, index), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})

    @router.get("/jobs")
    def jobs():
        return manager.list_jobs()

    @router.get("/jobs/{job_id}")
    def job(job_id: str):
        return call(manager.job, job_id)

    @router.post("/jobs/{job_id}/cancel", dependencies=mutation_dependencies)
    def cancel(job_id: str):
        return call(manager.cancel, job_id)

    @router.get("/jobs/{job_id}/video")
    def video(job_id: str):
        return FileResponse(call(manager.artifact, job_id, "video"), media_type="video/mp4")

    @router.get("/jobs/{job_id}/xmp")
    def xmp(job_id: str):
        return FileResponse(call(manager.artifact, job_id, "xmp"), media_type="application/zip", filename="timelapse-xmp.zip")

    return router
