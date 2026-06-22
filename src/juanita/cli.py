# SPDX-License-Identifier: GPL-3.0-or-later
"""
Turn YouTube cooking videos, recipe webpages, or local recipe text files into
Mealie recipes.

Pipeline per source:
  1. yt-dlp  -> title, description, thumbnail, auto-generated transcript
     (or, for a plain webpage, scrape its title/meta/text; for a local file,
     just read its text)
  2. Claude  -> structured recipe JSON (name, ingredients, steps, tags)
  3. Mealie  -> create recipe, fill in details, attach the source URL + thumbnail

Configuration is read from a .env file in the current directory (see
.env.example), or from real environment variables, which take precedence:
    ANTHROPIC_API_KEY, MEALIE_URL, MEALIE_TOKEN

Usage:
    cp .env.example .env   # then fill in your keys
    juanita https://youtu.be/wUewR4C0I_Y https://youtu.be/rzL07v6w8AA
    # import a local recipe text file (any positional that is a file on disk):
    juanita grandmas-walnut-bread.txt
    # or feed a file of URLs, one per line:
    juanita --from-file urls.txt
    # preview the parsed recipe without touching Mealie:
    juanita --dry-run https://youtu.be/UlafoXGyx6g
    # -v for full tracebacks, -q to only show warnings/errors
"""
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import stat
import sys
import time
from html.parser import HTMLParser
from pathlib import Path

import anthropic
import requests
from pydantic import BaseModel, Field
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

MODEL = "claude-opus-4-8"

log = logging.getLogger("juanita")


# ---- logging (fades style) --------------------------------------------------

FMT_SIMPLE = "*** juanita ***  %(asctime)s  %(levelname)-8s %(message)s"
FMT_DETAILED = "*** juanita ***  %(asctime)s  %(name)-18s %(levelname)-8s %(message)s"


def set_up_logging(verbose: bool, quiet: bool) -> None:
    """Configure the 'juanita' logger, mimicking fades' format and levels."""
    log.setLevel(logging.DEBUG)
    if verbose:
        level, fmt = logging.DEBUG, FMT_DETAILED
    elif quiet:
        level, fmt = logging.WARNING, FMT_SIMPLE
    else:
        level, fmt = logging.INFO, FMT_SIMPLE
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(fmt))
    log.addHandler(handler)


# ---- config -----------------------------------------------------------------

# Settings the tool reads, in KEY=VALUE form (also valid environment variables).
SECRET_KEYS = ("ANTHROPIC_API_KEY", "MEALIE_TOKEN")


def user_config_path() -> Path:
    """Default per-user config file, honoring XDG_CONFIG_HOME.

    e.g. ~/.config/juanita/config.env
    """
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / "juanita" / "config.env"


def load_dotenv(path: Path) -> bool:
    """Minimal .env/config loader: KEY=VALUE lines, # comments, optional quotes.

    Does not override variables already set in the environment. Returns True if
    the file existed and was read.
    """
    if not path.exists():
        return False
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value[:1] in ("'", '"'):
            # Quoted value: take everything up to the matching closing quote and
            # ignore the rest (e.g. a trailing inline comment).
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            # Unquoted value: drop a trailing ' # inline comment', if any.
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)
    return True


def load_config(explicit: str | None) -> None:
    """Populate os.environ from a config file (real env vars still win).

    Resolution order, first hit wins for any given key (load_dotenv uses
    setdefault, so earlier sources take precedence):
      1. --config FILE, if given (must exist).
      2. ./.env in the current directory.
      3. The per-user config (see user_config_path()).
    """
    if explicit:
        path = Path(explicit).expanduser()
        if not load_dotenv(path):
            raise FileNotFoundError(f"config file not found: {path}")
        log.debug("loaded config from %s", path)
        return
    if load_dotenv(Path.cwd() / ".env"):
        log.debug("loaded config from ./.env")
    if load_dotenv(user_config_path()):
        log.debug("loaded config from %s", user_config_path())


def _prompt(label: str, *, secret: bool, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    ask = getpass.getpass if secret else input
    while True:
        value = ask(f"{label}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        if not secret:
            return ""  # allow leaving optional plain fields blank
        print("  (required — please enter a value)")


def init_config(path: Path | None) -> int:
    """Interactively create the config file, prompting for keys and secrets."""
    target = (Path(path).expanduser() if path else user_config_path())
    print(f"Creating juanita config at:\n  {target}\n")
    if target.exists():
        ans = input("File already exists. Overwrite? [y/N]: ").strip().lower()
        if ans not in ("y", "yes"):
            print("Aborted; existing config left untouched.")
            return 1

    print("Enter your settings (input for keys/tokens is hidden):\n")
    anthropic_key = _prompt("Anthropic API key", secret=True)
    mealie_url = _prompt("Mealie base URL (no trailing slash, blank to skip)",
                         secret=False).rstrip("/")
    mealie_token = ""
    if mealie_url:
        mealie_token = _prompt("Mealie API token", secret=True)
    cookies = _prompt("YouTube cookies-from-browser (e.g. firefox; blank for none)",
                      secret=False)

    lines = [
        "# juanita config. Real environment variables override these.",
        "# Regenerate with: juanita init",
        "",
        f"ANTHROPIC_API_KEY={anthropic_key}",
    ]
    if mealie_url:
        lines += [f"MEALIE_URL={mealie_url}", f"MEALIE_TOKEN={mealie_token}"]
    if cookies:
        lines.append(f"YTDLP_COOKIES_FROM_BROWSER={cookies}")
    lines.append("")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines))
    target.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600 — it holds secrets
    print(f"\nWrote {target} (permissions 0600).")
    print("You can now run, e.g.:\n  juanita https://youtu.be/VIDEO_ID")
    return 0


# ---- 1. YouTube -------------------------------------------------------------

def fetch_video(url: str, *, cookies_from_browser: str | None = None,
                cookies_file: str | None = None) -> dict:
    """Return a source record {title, description, source_url, thumbnail, body}.

    Passing browser cookies makes YouTube far less likely to 429 the caption
    download (authenticated requests are throttled much less aggressively).
    """
    opts = {
        "skip_download": True,
        "writeautomaticsub": True,
        "writesubtitles": True,
        "subtitleslangs": ["en", "en-US", "en-orig"],
        "quiet": True,
        "no_warnings": True,
        # We only want metadata + captions, never the media. Without this,
        # extract_info still tries to *select* a playable format and raises
        # "Requested format is not available" for videos whose formats yt-dlp
        # can't enumerate (live/upcoming, DRM, login-walled, etc.).
        "ignore_no_formats_error": True,
    }
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if cookies_file:
        opts["cookiefile"] = cookies_file
    log.debug("yt-dlp: extracting metadata for %s", url)
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        # yt-dlp falls back to its "generic" extractor for any URL no site-
        # specific extractor claims, grabbing whatever embeddable media it can
        # find on the page (e.g. a background demo clip) rather than failing
        # outright. That gives a bogus title and an empty transcript for an
        # ordinary recipe page, so treat it the same as "not a video site" and
        # let fetch_source() fall back to fetch_webpage instead.
        if info.get("extractor_key") == "Generic":
            raise DownloadError(f"{url}: no dedicated video extractor (generic page)")
        # Fetch captions inside the ydl session so the request carries yt-dlp's
        # headers/cookies — a bare urllib fetch gets 429'd by YouTube fast.
        transcript = _extract_transcript(ydl, info)

    return {
        "title": info.get("title", ""),
        "description": info.get("description", "") or "",
        "source_url": info.get("webpage_url", url),
        "thumbnail": info.get("thumbnail"),
        "body": transcript,
    }


def fetch_source(url: str, *, cookies_from_browser: str | None = None,
                 cookies_file: str | None = None) -> dict:
    """Fetch a URL as a video first, falling back to a plain webpage.

    Most recipe sites aren't videos, so yt-dlp raises DownloadError for them
    (no extractor recognizes the URL) before ever touching captions/cookies —
    that's the signal to retry as a generic page. A real video-site failure
    (e.g. the friendly 429 RuntimeError from _download_caption) is not a
    DownloadError and propagates as-is.
    """
    try:
        return fetch_video(url, cookies_from_browser=cookies_from_browser,
                           cookies_file=cookies_file)
    except DownloadError:
        log.debug("yt-dlp doesn't recognize %s as a video; trying it as a webpage", url)
        return fetch_webpage(url)


# ---- 1b. Raw recipe text (local files, pasted text, ...) -------------------

def text_to_source_record(text: str, *, default_title: str = "") -> dict:
    """Build a source record straight from raw recipe text, no file or URL.

    The whole text is the body; the first non-blank line seeds the title
    (Claude still produces the final recipe name). There's no source URL or
    thumbnail. Used both for local text files and pasted text (e.g. the web
    frontend).
    """
    text = text.strip()
    title = next((ln.strip() for ln in text.splitlines() if ln.strip()), default_title)
    return {
        "title": title,
        "description": "",
        "source_url": None,
        "thumbnail": None,
        "body": text,
    }


def load_text_file(path: str) -> dict:
    """Read a local recipe text file into the same record shape as fetch_video."""
    p = Path(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    record = text_to_source_record(text, default_title=p.stem)
    if not record["body"]:
        raise RuntimeError(f"file is empty: {path}")
    return record


# ---- 1c. Generic webpages ----------------------------------------------------

_BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; juanita-mealie recipe importer)"}


class _PageParser(HTMLParser):
    """Pulls <title>, <meta name/property> tags, and visible text out of HTML.

    Used to turn a plain recipe webpage into a source record without adding an
    HTML-parsing dependency: stdlib html.parser is enough for title/meta/text.
    """

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "template"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.meta: dict[str, str] = {}
        self._skip_depth = 0
        self._in_title = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "meta":
            attrs_d = dict(attrs)
            key, content = attrs_d.get("property") or attrs_d.get("name"), attrs_d.get("content")
            if key and content:
                self.meta[key] = content

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        text = data.strip()
        if text:
            self._chunks.append(text)

    def text(self) -> str:
        return "\n".join(self._chunks)


def fetch_webpage(url: str) -> dict:
    """Fetch a plain (non-video) recipe webpage into a source record.

    No yt-dlp/transcript involved: the page's visible text becomes `body` for
    Claude to extract a recipe from, and `og:image`/`twitter:image` (when
    present) becomes the thumbnail.
    """
    r = requests.get(url, headers=_BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    parser = _PageParser()
    parser.feed(r.text)
    title = parser.meta.get("og:title") or parser.title.strip() or url
    description = parser.meta.get("og:description") or parser.meta.get("description") or ""
    thumbnail = parser.meta.get("og:image") or parser.meta.get("twitter:image")
    return {
        "title": title.strip(),
        "description": description.strip(),
        "source_url": url,
        "thumbnail": thumbnail,
        "body": parser.text(),
    }


def _extract_transcript(ydl: YoutubeDL, info: dict) -> str:
    """Pull the plain-text transcript from yt-dlp's caption tracks."""
    tracks = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    for lang in ("en", "en-US", "en-orig"):
        for source in (tracks, auto):
            formats = source.get(lang)
            if not formats:  # missing or an empty list for this language
                continue
            fmt = next(
                (f for f in formats if f.get("ext") == "json3"),
                formats[0],
            )
            return _download_caption(ydl, fmt["url"], fmt.get("ext"))
    log.warning("no English captions found; proceeding without a transcript")
    return ""


def _download_caption(ydl: YoutubeDL, url: str, ext: str | None, attempts: int = 4) -> str:
    """GET a caption track through yt-dlp's HTTP client, retrying on 429."""
    delay = 3
    for attempt in range(1, attempts + 1):
        try:
            raw = ydl.urlopen(url).read().decode("utf-8", "replace")
            break
        except Exception as e:  # noqa: BLE001 - retry only on rate-limit
            # Prefer a structured HTTP status (yt-dlp/urllib expose .status or
            # .code); fall back to a substring match for wrapped errors.
            status = getattr(e, "status", None) or getattr(e, "code", None)
            if status != 429 and "429" not in str(e):
                raise
            if attempt < attempts:
                log.warning("caption fetch rate-limited (429); retry %d/%d in %ds",
                            attempt, attempts - 1, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(
                "YouTube rate-limited the caption download (HTTP 429) after "
                f"{attempts} attempts. This is usually transient — retry in a few "
                "minutes, or pass browser cookies to authenticate the request: "
                "--cookies-from-browser firefox|chrome|... (or --cookies cookies.txt)."
            ) from e
    if ext == "json3":
        data = json.loads(raw)
        parts = []
        for event in data.get("events", []):
            for seg in event.get("segs", []) or []:
                parts.append(seg.get("utf8", ""))
        return "".join(parts).strip()
    # vtt/srt fallback: drop cue headers and timestamps
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.isdigit() or line == "WEBVTT":
            continue
        lines.append(line)
    return "\n".join(lines)


# ---- 2. Claude --------------------------------------------------------------

class Ingredient(BaseModel):
    quantity: float | None = Field(
        description="Numeric amount as a decimal (e.g. 365, 1, 0.5), or null when "
                    "there's no amount or it's 'to taste'. For a range like '2 or 3', "
                    "use the lower number (2) and add only the extra ('or 3') to note.")
    unit: str = Field(
        description="Unit of measure as written, singular, e.g. 'g', 'taza', 'cda', "
                    "'cup'. Empty string when there is no unit.")
    food: str = Field(
        description="The core ingredient itself, without quantity or unit, e.g. "
                    "'harina leudante', 'azúcar', 'walnuts'. Singular where natural.")
    note: str = Field(
        description="Only the extra qualifier, e.g. 'a gusto', 'finely chopped', "
                    "'or 3', 'room temperature'. Never repeat the quantity, unit, or "
                    "food here. Empty string when there is none.")


class Recipe(BaseModel):
    name: str = Field(description="Short recipe title, e.g. 'Creamy Sesame Ginger Dressing'")
    description: str = Field(description="One or two appetizing sentences. No URLs.")
    recipe_yield: str = Field(description="Yield/servings, e.g. '4 servings' or '1 jar'")
    ingredients: list[Ingredient] = Field(
        description="Each ingredient split into quantity, unit, food, and note")
    instructions: list[str] = Field(description="Ordered steps, one per item")
    tags: list[str] = Field(description="3-6 lowercase kebab-case tags")


def extract_recipe(client: anthropic.Anthropic, source: dict) -> Recipe:
    body = source["body"] or "(no source text available)"
    description = source.get("description") or ""
    prompt = (
        "You are extracting a clean, cookable recipe from the source material below "
        "(a cooking-video transcript or someone's written recipe notes).\n"
        "Use the SOURCE as the primary content; the title and description add context.\n"
        "Infer reasonable quantities only when the source clearly implies them; otherwise "
        "describe the ingredient without a fabricated amount. Do not invent steps.\n"
        "Write the recipe in the same language as the source.\n\n"
        f"TITLE: {source['title']}\n\n"
        f"DESCRIPTION:\n{description[:4000]}\n\n"
        f"SOURCE:\n{body[:120000]}"
    )
    log.info("calling Claude (%s, %d source chars)...", MODEL, len(body))
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": prompt}],
        output_format=Recipe,
    )
    log.debug(
        "claude: stop_reason=%s, tokens in=%s out=%s",
        resp.stop_reason, resp.usage.input_tokens, resp.usage.output_tokens,
    )
    if resp.parsed_output is None:
        raise RuntimeError(
            f"Claude did not return a parseable recipe (stop_reason={resp.stop_reason})")
    return resp.parsed_output


# ---- 3. Mealie --------------------------------------------------------------

def _check(r: requests.Response) -> requests.Response:
    """raise_for_status, but surface Mealie's JSON error body in the message."""
    if not r.ok:
        try:
            detail = json.dumps(r.json(), indent=2, ensure_ascii=False)
        except ValueError:
            detail = r.text[:1000]
        raise RuntimeError(f"{r.request.method} {r.url} -> {r.status_code}\n{detail}")
    return r


class Mealie:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {token}"
        self._tag_cache: dict[str, dict] = {}
        self._food_cache: dict[str, dict] = {}
        self._unit_cache: dict[str, dict] = {}
        self._units_loaded = False
        self._group_slug: str | None = None
        self._group_slug_loaded = False

    def group_slug(self) -> str | None:
        """The current user's group slug, used to build recipe page URLs.

        Mealie recipe pages live at /g/{group}/r/{slug}; the group varies per
        instance/user, so resolve it once (best-effort) rather than assuming
        the default 'home'.
        """
        if not self._group_slug_loaded:
            try:
                self._group_slug = _check(
                    self.s.get(f"{self.base}/api/groups/self", timeout=30)
                ).json().get("slug")
            except Exception as e:  # noqa: BLE001 - URL building is best-effort
                log.debug("could not resolve group slug for the recipe URL: %s", e)
            self._group_slug_loaded = True
        return self._group_slug

    def create(self, name: str) -> str:
        r = self.s.post(f"{self.base}/api/recipes", json={"name": name}, timeout=30)
        return _check(r).json()  # endpoint returns the new slug as a JSON string

    def get(self, slug: str) -> dict:
        return _check(self.s.get(f"{self.base}/api/recipes/{slug}", timeout=30)).json()

    def update(self, slug: str, recipe: dict) -> dict:
        return _check(self.s.put(f"{self.base}/api/recipes/{slug}", json=recipe, timeout=30)).json()

    def resolve_tag(self, name: str) -> dict:
        """Return a full Mealie tag object, creating it if it doesn't exist.

        Recipes can only reference existing organizer tags; passing a bare
        {name, slug} in the recipe PUT makes Mealie fail with the misleading
        "Recipe already exists". So create/look up the tag here and attach the
        full object (with id and groupId) instead.
        """
        key = name.lower()
        if key in self._tag_cache:
            return self._tag_cache[key]
        created = self.s.post(f"{self.base}/api/organizers/tags", json={"name": name}, timeout=30)
        if created.status_code == 201:
            tag = created.json()
        else:
            # Most likely it already exists -> look it up by name.
            found = _check(
                self.s.get(
                    f"{self.base}/api/organizers/tags",
                    params={"search": name, "perPage": 100},
                    timeout=30,
                )
            ).json()
            tag = next((t for t in found.get("items", []) if t["name"].lower() == key), None)
            if tag is None:
                _check(created)  # not a conflict either -> raise the real error
                raise RuntimeError(f"could not resolve tag {name!r}")
        self._tag_cache[key] = tag
        return tag

    def resolve_food(self, name: str) -> dict | None:
        """Return a Mealie food object for `name`, reusing an existing one
        (matched case-insensitively on name/plural/alias) or creating it.

        Linking ingredients to real foods is what makes them aggregate in
        shopping lists and show up in the foods database, instead of being just
        free text.
        """
        key = name.strip().lower()
        if not key:
            return None
        if key in self._food_cache:
            return self._food_cache[key]
        found = _check(self.s.get(
            f"{self.base}/api/foods", params={"search": name, "perPage": 50}, timeout=30,
        )).json()
        food = next((f for f in found.get("items", []) if self._food_matches(f, key)), None)
        if food is None:
            food = _check(self.s.post(
                f"{self.base}/api/foods", json={"name": name}, timeout=30,
            )).json()
        self._food_cache[key] = food
        return food

    @staticmethod
    def _food_matches(f: dict, key: str) -> bool:
        names = [f.get("name"), f.get("pluralName")]
        names += [a.get("name") for a in (f.get("aliases") or [])]
        return any(n and n.strip().lower() == key for n in names)

    def resolve_unit(self, name: str) -> dict | None:
        """Return a Mealie unit object for `name`, reusing an existing one
        (matched on name/plural/abbreviation/alias) or creating it."""
        key = name.strip().lower()
        if not key:
            return None
        self._ensure_units_loaded()
        if key in self._unit_cache:
            return self._unit_cache[key]
        unit = _check(self.s.post(
            f"{self.base}/api/units", json={"name": name}, timeout=30,
        )).json()
        self._unit_cache[key] = unit
        return unit

    def _ensure_units_loaded(self) -> None:
        """Index every existing unit by its names/abbreviations (there are few)."""
        if self._units_loaded:
            return
        items = _check(self.s.get(
            f"{self.base}/api/units", params={"perPage": 1000}, timeout=30,
        )).json().get("items", [])
        for u in items:
            keys = [u.get("name"), u.get("pluralName"),
                    u.get("abbreviation"), u.get("pluralAbbreviation")]
            keys += [a.get("name") for a in (u.get("aliases") or [])]
            for k in keys:
                if k and k.strip():
                    self._unit_cache.setdefault(k.strip().lower(), u)
        self._units_loaded = True

    def set_image(self, slug: str, content: bytes, extension: str) -> None:
        """Upload image bytes directly (PUT, multipart), instead of POSTing the
        URL and having Mealie fetch it server-side.

        Some sites' anti-bot protection 403s Mealie's own outbound fetch (its
        HTTP client looks nothing like a browser) even though the same URL is
        fine for a normal client — see _download_image.
        """
        files = {"image": (f"image.{extension}", content)}
        r = self.s.put(
            f"{self.base}/api/recipes/{slug}/image",
            files=files, data={"extension": extension}, timeout=60,
        )
        _check(r)


def _format_quantity(quantity: float | None) -> str:
    """Render a quantity without a trailing '.0' (365.0 -> '365', 0.5 -> '0.5')."""
    if quantity is None:
        return ""
    return str(int(quantity)) if float(quantity).is_integer() else str(quantity)


def _ingredient_original_text(ing: Ingredient) -> str:
    """Human-readable line, used as Mealie's originalText / display fallback."""
    parts = [p for p in (_format_quantity(ing.quantity), ing.unit, ing.food) if p]
    text = " ".join(parts)
    if ing.note:
        text = f"{text} ({ing.note})" if text else ing.note
    return text


def _build_recipe_ingredient(mealie: Mealie, ing: Ingredient, *, link: bool = True) -> dict:
    """Turn an extracted Ingredient into a Mealie recipeIngredient object.

    With `link` (the default), food and unit are resolved to real database
    objects (best-effort: if Mealie rejects one, fall back to an unlinked but
    still-amounted line). Without it, the ingredient is stored as a single
    plain-text line, touching nothing in the Mealie database.
    """
    if not link:
        return {"note": _ingredient_original_text(ing)}
    food = unit = None
    try:
        food = mealie.resolve_food(ing.food)
        unit = mealie.resolve_unit(ing.unit)
    except Exception as e:  # noqa: BLE001 - never let one ingredient sink the import
        log.warning("could not link food/unit for %r: %s", ing.food or ing.note, e)
    return {
        "quantity": ing.quantity,
        "unit": unit,
        "food": food,
        "note": ing.note or "",
        "originalText": _ingredient_original_text(ing),
    }


_IMAGE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
}


def _download_image(url: str) -> tuple[bytes, str]:
    """Download a thumbnail ourselves, for set_image to upload as a file.

    Mealie's own POST .../image {url} fetch is done by its HTTP client, which
    some sites' anti-bot protection 403s even though the same URL is fine for
    a normal client (e.g. with a browser User-Agent) — see _BROWSER_HEADERS.
    """
    r = requests.get(url, headers=_BROWSER_HEADERS, timeout=30)
    r.raise_for_status()
    content_type = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
    return r.content, _IMAGE_EXTENSIONS.get(content_type, "jpg")


def push_to_mealie(mealie: Mealie, recipe: Recipe, source: dict, *,
                   include_tags: bool = True, link_ingredients: bool = True) -> str:
    slug = mealie.create(recipe.name)
    log.debug("mealie: created recipe slug %s", slug)
    doc = mealie.get(slug)

    source_url = source.get("source_url")
    description = recipe.description.strip()
    if source_url and source_url not in description:
        description = f"{description}\n\nSource: {source_url}".strip()

    # NB: don't overwrite doc["name"] — Mealie already set it from create() and
    # may have de-duplicated it (e.g. "Foo (1)"). Re-setting the base name forces
    # a re-slug to an already-taken slug -> 400 "Recipe already exists".
    doc["description"] = description
    doc["recipeYield"] = recipe.recipe_yield
    if source_url:
        doc["orgURL"] = source_url
    doc["recipeIngredient"] = [
        _build_recipe_ingredient(mealie, i, link=link_ingredients) for i in recipe.ingredients
    ]
    log.debug("mealie: %s %d ingredients",
              "linked" if link_ingredients else "added (unlinked)", len(recipe.ingredients))
    doc["recipeInstructions"] = [{"text": s} for s in recipe.instructions]
    if include_tags and recipe.tags:
        doc["tags"] = [mealie.resolve_tag(t) for t in recipe.tags]
        log.debug("mealie: attached tags %s", [t["name"] for t in doc["tags"]])

    mealie.update(slug, doc)

    if source.get("thumbnail"):
        try:
            content, ext = _download_image(source["thumbnail"])
            mealie.set_image(slug, content, ext)
            log.debug("mealie: set image (.%s, %d bytes) from %s",
                      ext, len(content), source["thumbnail"])
        except Exception as e:  # noqa: BLE001 - image is best-effort
            log.warning("could not set image for %s: %s", slug, e)

    return slug


# ---- CLI --------------------------------------------------------------------

def _init_command(argv: list[str]) -> int:
    ip = argparse.ArgumentParser(
        prog="juanita init",
        description="Interactively create the config file (stored in your home by default).",
    )
    ip.add_argument("-c", "--config", metavar="FILE",
                    help="Write the config to FILE instead of the default user path")
    args = ip.parse_args(argv)
    set_up_logging(False, False)
    return init_config(Path(args.config) if args.config else None)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # `juanita init [...]` creates the config file and exits.
    if argv and argv[0] == "init":
        return _init_command(argv[1:])

    ap = argparse.ArgumentParser(
        description="Import recipes into Mealie from YouTube videos, recipe webpages, "
                    "or local text files.")
    ap.add_argument("urls", nargs="*", metavar="SOURCE",
                    help="YouTube/recipe-webpage URLs and/or paths to local recipe text files")
    ap.add_argument("-c", "--config", metavar="FILE",
                    help="Path to a config file (KEY=VALUE). Run `juanita init` "
                         "to create one. Defaults to ./.env then the per-user config.")
    ap.add_argument("--from-file", help="Read URLs from a file, one per line")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the parsed recipe, don't push to Mealie")
    ap.add_argument("--no-tags", action="store_true", help="Don't attach tags to recipes")
    ap.add_argument("--not-linked-ingredients", action="store_true",
                    help="Store ingredients as plain-text lines instead of linking each "
                         "food/unit to the Mealie database (creates nothing in it)")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER",
                    help="Load YouTube cookies from a browser (firefox, chrome, ...) "
                         "to avoid caption-download rate limits (HTTP 429)")
    ap.add_argument("--cookies", metavar="FILE",
                    help="Path to a cookies.txt file (alternative to --cookies-from-browser)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Verbose logging (full tracebacks)")
    ap.add_argument("-q", "--quiet", action="store_true", help="Only log warnings and errors")
    args = ap.parse_args(argv)

    set_up_logging(args.verbose, args.quiet)

    # Load config: --config FILE, else ./.env, else the per-user config.
    # Real environment variables always take precedence.
    try:
        load_config(args.config)
    except FileNotFoundError as e:
        ap.error(str(e))

    # Cookies: CLI flag wins, else YTDLP_COOKIES_FROM_BROWSER from env/.env.
    cookies_from_browser = args.cookies_from_browser or os.environ.get("YTDLP_COOKIES_FROM_BROWSER")

    sources = list(args.urls)
    if args.from_file:
        try:
            with open(args.from_file) as f:
                sources += [s for ln in f if (s := ln.strip()) and not s.startswith("#")]
        except OSError as e:
            ap.error(f"could not read --from-file {args.from_file}: {e}")
    if not sources:
        ap.error("no inputs given (pass YouTube URLs and/or local recipe text files)")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        ap.error("ANTHROPIC_API_KEY is not set. Run `juanita init` to create a "
                 "config, or set it in the environment / a --config file.")
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    mealie = None
    if not args.dry_run:
        base, token = os.environ.get("MEALIE_URL"), os.environ.get("MEALIE_TOKEN")
        if not base or not token:
            ap.error("set MEALIE_URL and MEALIE_TOKEN (run `juanita init`, "
                     "use a --config file, or pass --dry-run)")
        mealie = Mealie(base, token)

    failures = 0
    for item in sources:
        try:
            log.info("processing %s", item)
            # A local recipe text file vs. a URL to fetch with yt-dlp.
            if os.path.isfile(item):
                source = load_text_file(item)
            else:
                source = fetch_source(item, cookies_from_browser=cookies_from_browser,
                                      cookies_file=args.cookies)
            log.info("source: %r (%d chars)", source["title"], len(source["body"]))
            recipe = extract_recipe(client, source)
            log.info(
                "recipe: %r (%d ingredients, %d steps)",
                recipe.name, len(recipe.ingredients), len(recipe.instructions),
            )
            if args.dry_run:
                print(json.dumps(recipe.model_dump(), indent=2, ensure_ascii=False))
                continue
            slug = push_to_mealie(
                mealie, recipe, source,
                include_tags=not args.no_tags,
                link_ingredients=not args.not_linked_ingredients,
            )
            group = mealie.group_slug()
            if group:
                log.info("imported -> %s/g/%s/r/%s", mealie.base, group, slug)
            else:
                log.info("imported -> %s (recipe slug: %s)", mealie.base, slug)
        except Exception as e:  # noqa: BLE001 - keep going through the batch
            failures += 1
            log.error("failed on %s: %s", item, e)
            log.debug("traceback", exc_info=True)

    if failures:
        log.warning("%d of %d failed", failures, len(sources))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
