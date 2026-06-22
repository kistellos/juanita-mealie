# CLAUDE.md

Guidance for Claude Code (and humans) working in this repository.

## What this is

`juanita` (distribution `juanita-mealie`) is a CLI that imports recipes into
[Mealie](https://mealie.io) from different sources — currently YouTube cooking
videos (via `yt-dlp` transcripts), plain recipe webpages (scraped directly),
and local recipe text files. Claude extracts a structured recipe and it's
created via the Mealie REST API, with foods/units linked and the source URL +
thumbnail attached when available.

The name honors Juanita Bordoy (1916–1995), Doña Petrona's TV kitchen assistant
who did the prep and the dirty work in the background — see the "The name"
section in the README, which also names the patrona/domestic-worker inequality
behind it and cites Rebekah Pite's article. Keep that framing (affectionate, not
flattening) if you touch the branding. Keep the pipeline source-agnostic too: new
input types (a photo, a PDF, …) should plug into the same source-record →
extract → push flow.

## Layout

```
src/juanita/
  __init__.py   # package version + public re-exports
  __main__.py   # `python -m juanita`
  cli.py        # everything: fetch -> extract -> push, plus argparse main()
pyproject.toml  # packaging (hatchling, src layout), console script, ruff config
```

The console script `juanita` maps to `juanita.cli:main`.

## Pipeline (all in `cli.py`)

Each input is loaded into a common **source record** dict —
`{title, description, source_url, thumbnail, body}` — consumed by steps 2–3.
`main()` auto-detects per positional: `os.path.isfile(item)` → `load_text_file`
(a local recipe text file: `body` = file text, no `source_url`/`thumbnail`),
otherwise → `fetch_source` (a URL).

1. **Fetch**
   - `fetch_source` — tries `fetch_video` first; if yt-dlp raises
     `DownloadError` (no extractor recognizes the URL — i.e. it's not a video
     site), falls back to `fetch_webpage`. `fetch_video` itself raises that
     same `DownloadError` when yt-dlp resolves to its **generic** extractor
     (`info["extractor_key"] == "Generic"`): the generic extractor grabs
     whatever embeddable media it can find on an ordinary page (e.g. a
     background demo clip) instead of failing outright, which would otherwise
     produce a bogus title and an empty transcript for a plain recipe page —
     so it's treated as "not really a video" too. A real video-site failure
     (e.g. the friendly 429 `RuntimeError` from `_download_caption`) is not a
     `DownloadError` and propagates instead of falling back.
   - `fetch_video` — `yt-dlp` extracts metadata without downloading the
     video: `title`, `description`, `source_url`, `thumbnail`, and the
     auto-generated captions as `body`. The transcript is parsed from the `json3` caption
     track (`_extract_transcript` / `_download_caption`), preferring `en`, then
     `en-US`, then `en-orig`, with a VTT/SRT fallback. Captions are fetched through
     `ydl.urlopen` (yt-dlp's HTTP client, with its headers/cookies) rather than a
     bare `urllib` request, and `_download_caption` retries on `HTTP 429` with
     exponential backoff. Optional browser cookies (`cookiesfrombrowser` /
     `cookiefile`) authenticate the request to dodge sustained 429s; on a final
     429 it raises a friendly error pointing at the cookie flags.
   - `fetch_webpage` — for a plain (non-video) URL: a stdlib `html.parser`
     subclass (`_PageParser`) pulls `<title>`, `<meta>` tags, and visible text
     out of the page. `title` prefers `og:title`, `description` prefers
     `og:description`, `thumbnail` is `og:image` (falling back to
     `twitter:image`); `body` is the page's stripped visible text (script/style/
     svg/template content excluded). No new dependency — deliberately not
     BeautifulSoup, per the project's lean-deps convention.

2. **Extract** (`extract_recipe`) — Claude (`claude-opus-4-8`, adaptive thinking,
   `effort: high`) turns `body` + title + description into a validated `Recipe`
   via `messages.parse()` with a Pydantic schema (structured outputs): `name`,
   `description`, `recipe_yield`, `ingredients[]`, `instructions[]`, `tags[]`.
   Each ingredient is a structured `Ingredient` (`quantity`, `unit`, `food`,
   `note`), not a free-text line, so it can be linked to Mealie's database.
   The prompt is source-agnostic (transcript or written notes), forbids inventing
   steps or fabricating quantities, and asks for the recipe in the source's
   language. We deliberately do **not** mention Mealie or pass an example: the
   Pydantic schema is the contract, and Mealie shaping happens in step 3.

3. **Push** (`push_to_mealie`) — against the Mealie REST API:
   - `POST /api/recipes` `{name}` → returns the new slug.
   - `GET /api/recipes/{slug}` → full recipe document.
   - Merge: set description/yield; build each `recipeIngredient` from an
     `Ingredient` (`_build_recipe_ingredient`) with `quantity`, `note`,
     `originalText`, and **linked** `food`/`unit` objects via `resolve_food` /
     `resolve_unit` (search-or-create against `/api/foods` and `/api/units`,
     cached; units are matched on name/abbreviation/alias). Linking is
     best-effort — a failure falls back to an unlinked but still-amounted line.
     `--not-linked-ingredients` (`link_ingredients=False`) stores each ingredient
     as a single plain-text line and touches nothing in the database.
     Map instructions to `[{text}]`, resolve tags to full organizer-tag objects.
     When the record has a `source_url`, set `orgURL` and append `Source: <url>`
     to the description; both are skipped for local files (no URL).
   - `PUT /api/recipes/{slug}` with the merged document.
   - When a `thumbnail` is present: `_download_image` fetches it ourselves
     (`requests`, browser-like `_BROWSER_HEADERS`) and `Mealie.set_image`
     uploads the bytes via `PUT /api/recipes/{slug}/image` (multipart,
     `image` + `extension` fields) — deliberately *not* the `POST .../image
     {url}` "fetch it yourself" endpoint, because some sites' anti-bot
     protection 403s Mealie's own outbound fetch (its HTTP client looks
     nothing like a browser) even though the same URL is fine for juanita's
     request. Failure at either step (download or upload) is non-fatal —
     logged/skipped.

## Configuration

`load_config()` populates `os.environ` from the first config source that exists,
using `setdefault` so real environment variables always win:

1. `--config FILE` / `-c FILE` (must exist, else `ap.error`).
2. `./.env` in the current directory.
3. `user_config_path()` — `$XDG_CONFIG_HOME/juanita/config.env`,
   defaulting to `~/.config/juanita/config.env`.

All files share the same `KEY=VALUE` format (`load_dotenv`). The `init`
subcommand (`juanita init`, handled in `main()` before argparse via
`_init_command` → `init_config`) interactively prompts and writes the per-user
config with mode `0600` (it holds secrets — `SECRET_KEYS` are read via
`getpass`). `init` accepts `-c FILE` to write elsewhere.

Settings:

- `ANTHROPIC_API_KEY` — required.
- `MEALIE_URL` — required unless `--dry-run`.
- `MEALIE_TOKEN` — required unless `--dry-run`; a long-lived Mealie API token.
- `YTDLP_COOKIES_FROM_BROWSER` — optional; browser name for cookie loading.

## Conventions & decisions

- Secrets live only in `.env`, which is gitignored. Never commit real keys; keep
  `.env.example` placeholder-only.
- `src/` layout, packaged with hatchling. Keep the public API re-exported from
  `__init__.py` in sync when adding top-level functions.
- `Recipe`'s Pydantic schema must use only structured-output-safe constructs:
  strings, string arrays, nullable numbers, and nested models (e.g. `Ingredient`)
  are fine; avoid min/max length and other value constraints.
- Food/unit linking creates entries in the user's Mealie database when no match
  exists. Matching is case-insensitive exact (name/plural/abbreviation/alias) to
  reuse existing foods/units rather than spawning near-duplicates.
- Idempotency is **not** handled — re-importing a video creates a duplicate
  (Mealie appends `-1`, `-2`, … to the slug).
- When updating a recipe, don't overwrite `doc["name"]`: Mealie set it on
  create and may have de-duplicated it; re-setting forces a re-slug to a taken
  slug → 400 "Recipe already exists".
- Direct API writes, not the Mealie zip/migration importer.

## Dev workflow

Use [fades](https://github.com/PyAr/fades) — `bin/run_dev` installs this project
editable in a managed venv and forwards its args, so `src/` edits are picked up
on the next run:

```bash
bin/run_dev --dry-run https://youtu.be/VIDEO_ID   # no Mealie creds needed
bin/run_tests                                     # run the test suite (pytest)
fades -d ruff -x ruff -- check src tests          # lint
```

`bin/run_dev` declares `-d requests` only to trigger fades' install step and
passes `--pip-options="-e ."`; `FADES_REBUILD=1 bin/run_dev ...` forces a fresh
venv after adding a dependency to `pyproject.toml`. `bin/run_tests` works the
same way (`-d pytest`, `-e .`) and forwards its args to pytest, e.g.
`bin/run_tests -k linking`.

The `tests/` suite is fully offline (no network, no real Anthropic/Mealie
calls); CI runs ruff + pytest on every push/PR. `--dry-run` remains the quickest
end-to-end smoke check (it exercises fetch + extract without writing to Mealie).
