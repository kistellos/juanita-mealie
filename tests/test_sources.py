# SPDX-License-Identifier: GPL-3.0-or-later
"""Source loading: local text files, caption parsing/retry, and webpages."""
from __future__ import annotations

import json

import pytest
from yt_dlp.utils import DownloadError

from juanita import cli


class FakeResp:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class FakeYDL:
    """Fake YoutubeDL exposing just the urlopen used by _download_caption."""

    def __init__(self, payload: bytes = b"", exc: Exception | None = None):
        self._payload = payload
        self._exc = exc
        self.calls = 0

    def urlopen(self, url: str) -> FakeResp:
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return FakeResp(self._payload)


def test_load_text_file_uses_first_line_as_title(tmp_path):
    f = tmp_path / "recipe.txt"
    f.write_text("\n  Grandma's Bread  \n\n365 g flour\nwalnuts\n")

    rec = cli.load_text_file(str(f))
    assert rec["title"] == "Grandma's Bread"
    assert rec["source_url"] is None
    assert rec["thumbnail"] is None
    assert "365 g flour" in rec["body"]
    assert rec["body"].startswith("Grandma's Bread")  # stripped of leading blanks


def test_load_text_file_empty_raises(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("   \n\n")
    with pytest.raises(RuntimeError, match="empty"):
        cli.load_text_file(str(f))


def test_download_caption_json3():
    payload = json.dumps({
        "events": [
            {"segs": [{"utf8": "Hello "}, {"utf8": "world"}]},
            {"segs": [{"utf8": "!"}]},
            {"segs": None},
        ]
    }).encode()
    ydl = FakeYDL(payload=payload)

    assert cli._download_caption(ydl, "http://x", "json3") == "Hello world!"


def test_download_caption_vtt_strips_timestamps_and_headers():
    vtt = (
        "WEBVTT\n\n"
        "1\n00:00:00.000 --> 00:00:01.000\nHello\n\n"
        "2\n00:00:01.000 --> 00:00:02.000\nworld\n"
    )
    ydl = FakeYDL(payload=vtt.encode())

    assert cli._download_caption(ydl, "http://x", "vtt") == "Hello\nworld"


def test_download_caption_non_429_error_propagates():
    ydl = FakeYDL(exc=ValueError("boom"))
    with pytest.raises(ValueError, match="boom"):
        cli._download_caption(ydl, "http://x", "json3")


def test_download_caption_429_exhausts_with_friendly_error(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)  # don't actually wait
    ydl = FakeYDL(exc=Exception("HTTP Error 429: Too Many Requests"))

    with pytest.raises(RuntimeError, match="429"):
        cli._download_caption(ydl, "http://x", "json3", attempts=2)
    assert ydl.calls == 2  # retried the configured number of times


def test_extract_transcript_picks_json3_from_automatic_captions():
    payload = json.dumps({"events": [{"segs": [{"utf8": "hi"}]}]}).encode()
    ydl = FakeYDL(payload=payload)
    info = {"automatic_captions": {"en": [{"ext": "json3", "url": "http://x"}]}}

    assert cli._extract_transcript(ydl, info) == "hi"


def test_extract_transcript_no_captions_returns_empty():
    assert cli._extract_transcript(FakeYDL(), {}) == ""


def test_extract_transcript_empty_format_list_is_skipped():
    # A present language key with an empty format list must not IndexError; it
    # should fall through to the next source/language (here: none -> empty).
    info = {"subtitles": {"en": []}, "automatic_captions": {"en": []}}
    assert cli._extract_transcript(FakeYDL(), info) == ""


def test_download_caption_429_via_status_attribute(monkeypatch):
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    class HTTP429(Exception):
        status = 429  # structured status, message has no "429"

    ydl = FakeYDL(exc=HTTP429("rate limited"))
    with pytest.raises(RuntimeError, match="429"):
        cli._download_caption(ydl, "http://x", "json3", attempts=2)
    assert ydl.calls == 2


class FakeYoutubeDLCM:
    """Fake YoutubeDL context manager exposing just extract_info, for fetch_video."""

    def __init__(self, info: dict):
        self._info = info

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url: str, download: bool = False) -> dict:
        return self._info


def test_fetch_video_raises_for_generic_extractor(monkeypatch):
    # yt-dlp falls back to its "generic" extractor for any page no dedicated
    # site extractor claims; that's not a real video, so fetch_video should
    # raise DownloadError (the same signal as "URL unsupported") rather than
    # returning a bogus title with an empty transcript.
    info = {"extractor_key": "Generic", "title": "background-clip.MOV"}
    monkeypatch.setattr(cli, "YoutubeDL", lambda opts: FakeYoutubeDLCM(info))

    with pytest.raises(DownloadError):
        cli.fetch_video("https://example.com/recipe")


class FakeHTTPResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self) -> None:
        pass


PAGE_HTML = """
<html><head>
<title>Fallback Title</title>
<meta property="og:title" content="Lemongrass Chicken">
<meta property="og:description" content="Crispy rice paper rolls.">
<meta property="og:image" content="https://img.example/hero.jpg">
<script>var x = "not visible";</script>
<style>.a { color: red }</style>
</head><body>
<nav>Home</nav>
<h1>Lemongrass Chicken</h1>
<p>Mix 2 cups rice with lemongrass.</p>
</body></html>
"""


def test_fetch_webpage_extracts_title_description_image_and_text(monkeypatch):
    monkeypatch.setattr(cli.requests, "get", lambda url, **kw: FakeHTTPResponse(PAGE_HTML))

    rec = cli.fetch_webpage("https://example.com/recipe")

    assert rec["title"] == "Lemongrass Chicken"
    assert rec["description"] == "Crispy rice paper rolls."
    assert rec["thumbnail"] == "https://img.example/hero.jpg"
    assert rec["source_url"] == "https://example.com/recipe"
    assert "Mix 2 cups rice with lemongrass." in rec["body"]
    assert "not visible" not in rec["body"]  # script content excluded
    assert "color: red" not in rec["body"]  # style content excluded


def test_fetch_webpage_falls_back_to_title_tag_without_og_meta(monkeypatch):
    monkeypatch.setattr(
        cli.requests, "get",
        lambda url, **kw: FakeHTTPResponse("<html><head><title>Plain Page</title></head>"
                                           "<body><p>hi</p></body></html>"),
    )

    rec = cli.fetch_webpage("https://example.com/x")
    assert rec["title"] == "Plain Page"
    assert rec["thumbnail"] is None


class FakeImageResponse:
    def __init__(self, content: bytes, content_type: str):
        self.content = content
        self.headers = {"Content-Type": content_type}

    def raise_for_status(self) -> None:
        pass


def test_download_image_picks_extension_from_content_type(monkeypatch):
    monkeypatch.setattr(
        cli.requests, "get",
        lambda url, **kw: FakeImageResponse(b"\xff\xd8", "image/jpeg; charset=binary"),
    )
    content, ext = cli._download_image("https://example.com/hero.jpg")
    assert content == b"\xff\xd8"
    assert ext == "jpg"


def test_download_image_unknown_content_type_defaults_to_jpg(monkeypatch):
    monkeypatch.setattr(
        cli.requests, "get",
        lambda url, **kw: FakeImageResponse(b"...", "application/octet-stream"),
    )
    _content, ext = cli._download_image("https://example.com/hero")
    assert ext == "jpg"


def test_fetch_source_falls_back_to_webpage_on_download_error(monkeypatch):
    def fake_fetch_video(url, **kw):
        raise DownloadError("Unsupported URL")

    monkeypatch.setattr(cli, "fetch_video", fake_fetch_video)
    monkeypatch.setattr(cli, "fetch_webpage", lambda url: {"title": "from webpage"})

    assert cli.fetch_source("https://example.com/recipe") == {"title": "from webpage"}


def test_fetch_source_uses_video_when_yt_dlp_recognizes_it(monkeypatch):
    monkeypatch.setattr(cli, "fetch_video", lambda url, **kw: {"title": "from video"})
    monkeypatch.setattr(
        cli, "fetch_webpage",
        lambda url: pytest.fail("should not fall back when fetch_video succeeds"),
    )

    assert cli.fetch_source("https://youtu.be/abc") == {"title": "from video"}


def test_fetch_source_does_not_swallow_non_download_errors(monkeypatch):
    def fake_fetch_video(url, **kw):
        raise RuntimeError("HTTP 429")

    monkeypatch.setattr(cli, "fetch_video", fake_fetch_video)
    monkeypatch.setattr(
        cli, "fetch_webpage",
        lambda url: pytest.fail("should not fall back on a non-DownloadError failure"),
    )

    with pytest.raises(RuntimeError, match="429"):
        cli.fetch_source("https://youtu.be/abc")
