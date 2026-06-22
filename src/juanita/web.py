# SPDX-License-Identifier: GPL-3.0-or-later
"""Minimal web frontend for juanita: paste a URL or recipe text, get a Mealie recipe.

Wraps the existing CLI pipeline (fetch_video -> extract_recipe -> push_to_mealie)
behind a small FastAPI app. Extraction takes 30s+ (yt-dlp fetch + a Claude
call), so submissions run as background jobs that the page polls for status
rather than holding the HTTP connection open.

Configuration is the same as the CLI (see cli.py's module docstring), plus:
  JUANITA_WEB_TOKEN   - required; shared secret for HTTP Basic auth.
  YTDLP_COOKIES_FILE  - optional path to a cookies.txt (browser-cookie auth
                        doesn't work headless/in a container).

Run with the `juanita-web` console script, or directly via
`uvicorn juanita.web:app`.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import secrets
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock

import anthropic
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from juanita.cli import (
    Mealie,
    extract_recipe,
    fetch_video,
    load_config,
    push_to_mealie,
    text_to_source_record,
)

log = logging.getLogger("juanita.web")

MAX_JOBS = 50
MAX_WORKERS = 2

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
security = HTTPBasic()


@dataclasses.dataclass
class Job:
    id: str
    kind: str  # "url" | "text"
    source: str  # the submitted URL, or the full pasted text
    status: str = "queued"  # queued | running | done | error
    recipe_name: str | None = None
    mealie_link: str | None = None
    error: str | None = None
    created_at: float = dataclasses.field(default_factory=time.time)

    @property
    def preview(self) -> str:
        """A short display label: the URL as-is, or the first line of pasted text."""
        if self.kind == "url":
            return self.source
        first_line = next((ln.strip() for ln in self.source.splitlines() if ln.strip()), "")
        if len(first_line) > 60:
            return first_line[:60] + "…"
        return first_line or "(pasted text)"


def _job_dict(job: Job) -> dict:
    return {**dataclasses.asdict(job), "preview": job.preview}


class _JobStore:
    """In-memory job registry, capped at `max_jobs` (oldest evicted first)."""

    def __init__(self, max_jobs: int = MAX_JOBS):
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = Lock()
        self._max = max_jobs

    def add(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > self._max:
                self._jobs.popitem(last=False)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def recent(self, limit: int = 20) -> list[Job]:
        return list(reversed(list(self._jobs.values())))[:limit]


jobs = _JobStore()
executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

_client: anthropic.Anthropic | None = None
_mealie: Mealie | None = None
_cookies_file: str | None = None
_init_lock = Lock()


def _ensure_initialized() -> None:
    """Load config and build the shared Anthropic/Mealie clients, once.

    Mirrors the env-var checks in cli.py's main(), so misconfiguration fails
    fast at startup instead of on the first submitted job.
    """
    global _client, _mealie, _cookies_file
    if _mealie is not None:
        return
    with _init_lock:
        if _mealie is not None:
            return
        load_config(None)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        if not os.environ.get("JUANITA_WEB_TOKEN"):
            raise RuntimeError("JUANITA_WEB_TOKEN is not set (shared secret for the web UI).")
        base, token = os.environ.get("MEALIE_URL"), os.environ.get("MEALIE_TOKEN")
        if not base or not token:
            raise RuntimeError("MEALIE_URL and MEALIE_TOKEN must be set.")
        _client = anthropic.Anthropic()
        _mealie = Mealie(base, token)
        _cookies_file = os.environ.get("YTDLP_COOKIES_FILE")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_initialized()
    yield


app = FastAPI(title="juanita", lifespan=lifespan)


def _run_job(
    job: Job, client: anthropic.Anthropic, mealie: Mealie, cookies_file: str | None,
) -> None:
    job.status = "running"
    try:
        if job.kind == "text":
            source = text_to_source_record(job.source)
        else:
            source = fetch_video(job.source, cookies_file=cookies_file)
        recipe = extract_recipe(client, source)
        job.recipe_name = recipe.name
        slug = push_to_mealie(mealie, recipe, source)
        group = mealie.group_slug()
        job.mealie_link = (
            f"{mealie.base}/g/{group}/r/{slug}" if group else f"{mealie.base} (slug: {slug})"
        )
        job.status = "done"
    except Exception as e:  # noqa: BLE001 - surfaced to the UI, not raised
        job.error = str(e)
        job.status = "error"
        log.error("import failed for %s job %r: %s", job.kind, job.preview, e)


def _check_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:  # noqa: B008
    token = os.environ.get("JUANITA_WEB_TOKEN")
    if not token or not secrets.compare_digest(credentials.password, token):
        raise HTTPException(
            status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"},
        )


class SubmitRequest(BaseModel):
    url: str | None = None
    text: str | None = None


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _: None = Depends(_check_auth)):
    return templates.TemplateResponse(request, "index.html", {"jobs": jobs.recent()})


@app.post("/jobs")
def submit(body: SubmitRequest, _: None = Depends(_check_auth)):
    url = (body.url or "").strip()
    text = (body.text or "").strip()
    if bool(url) == bool(text):  # exactly one of the two must be given
        raise HTTPException(status_code=400, detail="provide exactly one of url or text")
    kind = "url" if url else "text"
    job = Job(id=uuid.uuid4().hex[:12], kind=kind, source=url or text)
    jobs.add(job)
    executor.submit(_run_job, job, _client, _mealie, _cookies_file)
    return {"id": job.id}


@app.get("/jobs/{job_id}")
def job_status(job_id: str, _: None = Depends(_check_auth)):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    return _job_dict(job)


@app.get("/jobs")
def list_jobs(_: None = Depends(_check_auth)):
    """Recent jobs, for the page to refresh its history table without a reload."""
    return {"jobs": [_job_dict(j) for j in jobs.recent()]}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s")
    uvicorn.run(app, host="0.0.0.0", port=8000)  # noqa: S104 - intentional container bind


if __name__ == "__main__":
    main()
