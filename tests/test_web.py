# SPDX-License-Identifier: GPL-3.0-or-later
"""juanita.web: auth gate, job submission, polling to done/error. Offline."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from juanita import web
from juanita.cli import Ingredient, Recipe

AUTH = ("juanita", "s3cret")


@pytest.fixture
def client(monkeypatch, fake_mealie):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MEALIE_URL", "http://mealie.example.com")
    monkeypatch.setenv("MEALIE_TOKEN", "test-token")
    monkeypatch.setenv("JUANITA_WEB_TOKEN", "s3cret")
    monkeypatch.setattr(web, "_mealie", None)
    monkeypatch.setattr(web, "_client", None)
    with TestClient(web.app) as c:
        monkeypatch.setattr(web, "_mealie", fake_mealie)
        web.jobs._jobs.clear()
        yield c


def make_recipe(**over) -> Recipe:
    base = dict(
        name="Pan de nuez",
        description="Rico pan.",
        recipe_yield="1 pan",
        ingredients=[Ingredient(quantity=365, unit="g", food="harina", note="")],
        instructions=["Mezclar.", "Hornear."],
        tags=["pan"],
    )
    base.update(over)
    return Recipe(**base)


def source_record(**over) -> dict:
    src = {
        "title": "Pan",
        "description": "",
        "source_url": "https://youtu.be/abc",
        "thumbnail": None,
        "body": "...",
    }
    src.update(over)
    return src


def wait_for(client, job_id, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = client.get(f"/jobs/{job_id}", auth=AUTH)
        job = r.json()
        if job["status"] not in ("queued", "running"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


def test_index_requires_auth(client):
    assert client.get("/").status_code == 401


def test_index_rejects_wrong_password(client):
    r = client.get("/", auth=("juanita", "wrong"))
    assert r.status_code == 401


def test_index_accepts_correct_token(client):
    r = client.get("/", auth=AUTH)
    assert r.status_code == 200


def test_submit_and_poll_to_done(client, monkeypatch):
    monkeypatch.setattr(web, "fetch_video", lambda url, **kw: source_record(source_url=url))
    monkeypatch.setattr(web, "extract_recipe", lambda client, source: make_recipe())
    monkeypatch.setattr(web, "push_to_mealie", lambda mealie, recipe, source, **kw: "pan-de-nuez")

    r = client.post("/jobs", json={"url": "https://youtu.be/abc"}, auth=AUTH)
    assert r.status_code == 200
    job_id = r.json()["id"]

    job = wait_for(client, job_id)
    assert job["status"] == "done"
    assert job["recipe_name"] == "Pan de nuez"
    assert job["mealie_link"] == "http://mealie.example.com/g/home/r/pan-de-nuez"
    assert job["preview"] == "https://youtu.be/abc"


def test_submit_surfaces_pipeline_error(client, monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("yt-dlp exploded")

    monkeypatch.setattr(web, "fetch_video", boom)

    r = client.post("/jobs", json={"url": "https://youtu.be/bad"}, auth=AUTH)
    job_id = r.json()["id"]

    job = wait_for(client, job_id)
    assert job["status"] == "error"
    assert "yt-dlp exploded" in job["error"]


def test_submit_requires_exactly_one_of_url_or_text(client):
    assert client.post("/jobs", json={"url": "  "}, auth=AUTH).status_code == 400
    assert client.post("/jobs", json={}, auth=AUTH).status_code == 400
    assert client.post(
        "/jobs", json={"url": "https://youtu.be/abc", "text": "stuff"}, auth=AUTH,
    ).status_code == 400


def test_submit_text_and_poll_to_done(client, monkeypatch):
    fetch_calls = []
    monkeypatch.setattr(web, "fetch_video", lambda url, **kw: fetch_calls.append(url))
    monkeypatch.setattr(web, "extract_recipe", lambda client, source: make_recipe())
    monkeypatch.setattr(web, "push_to_mealie", lambda mealie, recipe, source, **kw: "pan-de-nuez")

    pasted = "Grandma's Bread\n\n365g flour\nwalnuts\n\nMix. Bake."
    r = client.post("/jobs", json={"text": pasted}, auth=AUTH)
    assert r.status_code == 200
    job_id = r.json()["id"]

    job = wait_for(client, job_id)
    assert job["status"] == "done"
    assert job["recipe_name"] == "Pan de nuez"
    assert job["preview"] == "Grandma's Bread"
    assert fetch_calls == []  # text jobs never touch yt-dlp


def test_unknown_job_404s(client):
    r = client.get("/jobs/does-not-exist", auth=AUTH)
    assert r.status_code == 404


def test_list_jobs_returns_recent(client, monkeypatch):
    monkeypatch.setattr(web, "fetch_video", lambda url, **kw: source_record(source_url=url))
    monkeypatch.setattr(web, "extract_recipe", lambda client, source: make_recipe())
    monkeypatch.setattr(web, "push_to_mealie", lambda mealie, recipe, source, **kw: "pan-de-nuez")

    job_id = client.post("/jobs", json={"url": "https://youtu.be/abc"}, auth=AUTH).json()["id"]
    wait_for(client, job_id)

    r = client.get("/jobs", auth=AUTH)
    assert r.status_code == 200
    listed = r.json()["jobs"]
    assert listed[0]["id"] == job_id
    assert listed[0]["status"] == "done"
