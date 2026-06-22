# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared fixtures and fakes for the juanita test suite.

Everything here is offline: no network, no real Anthropic/Mealie calls.
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_env():
    """Snapshot os.environ and restore it after each test.

    load_dotenv / load_config mutate os.environ directly (setdefault), so we
    can't rely on monkeypatch alone to undo them.
    """
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


class FakeMealie:
    """In-memory Mealie double recording everything push_to_mealie does."""

    def __init__(self):
        self.base = "http://mealie.example.com"
        self.created_name: str | None = None
        self.updated: dict | None = None
        self.images: list[tuple[str, str]] = []
        self.image_should_fail = False
        self.tag_calls: list[str] = []
        self.food_calls: list[str] = []
        self.unit_calls: list[str] = []

    def create(self, name: str) -> str:
        self.created_name = name
        return "the-slug"

    def get(self, slug: str) -> dict:
        # A freshly-created Mealie recipe document, deduped name and all.
        return {"name": self.created_name, "slug": slug, "description": ""}

    def update(self, slug: str, recipe: dict) -> dict:
        self.updated = recipe
        return recipe

    def resolve_tag(self, name: str) -> dict:
        self.tag_calls.append(name)
        return {"id": f"tag-{name}", "name": name}

    def resolve_food(self, name: str) -> dict | None:
        self.food_calls.append(name)
        return {"id": f"food-{name}", "name": name} if name.strip() else None

    def resolve_unit(self, name: str) -> dict | None:
        self.unit_calls.append(name)
        return {"id": f"unit-{name}", "name": name} if name.strip() else None

    def set_image_from_url(self, slug: str, image_url: str) -> None:
        if self.image_should_fail:
            raise RuntimeError("image boom")
        self.images.append((slug, image_url))

    def group_slug(self) -> str | None:
        return "home"


@pytest.fixture
def fake_mealie() -> FakeMealie:
    return FakeMealie()
