# Juanita

**Juanita** imports recipes into [Mealie](https://mealie.io) from wherever they
live. Point her at a YouTube cooking video, a recipe webpage, or a local text
file of notes and she does the prep: pulls the content (transcript via
[`yt-dlp`](https://github.com/yt-dlp/yt-dlp), the page's text for a plain
webpage, or just reads the file), has
[Claude](https://www.anthropic.com/claude) extract a structured recipe in the
source's own language, and creates it in Mealie via the API — ingredients linked
to your foods database, with the source URL and thumbnail attached when available.

> Named after [Juanita Bordoy](https://es.wikipedia.org/wiki/Juanita_Bordoy),
> Doña Petrona's assistant on Argentine TV, who for decades did the prep and the
> dirty work off to the side while the star took the spotlight. See
> [The name](#the-name) — it's worth knowing whose labor the name honors.

The PyPI distribution is `juanita-mealie`; the command you run is `juanita`.

## Install & run

Requires Python 3.11+.

### With fades (recommended)

[**fades**](https://github.com/PyAr/fades) creates and manages the virtual
environment for you, installing the dependency on first run — no global install,
no manual venv:

```bash
fades -d juanita-mealie -x juanita -- https://youtu.be/VIDEO_ID
```

`-d` declares the dependency and `-x` runs the installed `juanita` console
script inside the managed venv. A handy alias keeps invocations short:

```bash
alias juanita='fades -d juanita-mealie -x juanita --'
juanita --dry-run https://youtu.be/VIDEO_ID
```

### With pip

```bash
pip install juanita-mealie
juanita https://youtu.be/VIDEO_ID
```

> Not yet on PyPI? Install straight from source by replacing the dependency name
> with `git+https://github.com/gilgamezh/juanita-mealie` in any command above.

## Configure

The quickest way is the interactive setup, which writes a config file to your
home directory (`~/.config/juanita/config.env`, mode `0600`):

```bash
juanita init
# with fades: fades -d juanita-mealie -x juanita -- init
```

It prompts for your Anthropic API key, Mealie URL + token (optional — skip for
`--dry-run` only), and optional YouTube cookies. After that, just run the tool
and it picks up the config automatically.

Prefer to manage it yourself? Settings are read, in order (first hit wins; real
environment variables always override):

1. `--config FILE` / `-c FILE` — an explicit config file.
2. `./.env` in the current directory (see [`.env.example`](./.env.example)).
3. `~/.config/juanita/config.env` — the per-user config.

| Variable | Required | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Used for recipe extraction. <https://console.anthropic.com/settings/keys> |
| `MEALIE_URL` | unless `--dry-run` | Base URL of your Mealie instance, no trailing slash. |
| `MEALIE_TOKEN` | unless `--dry-run` | Long-lived token: Mealie UI → Profile → API Tokens → create. |
| `YTDLP_COOKIES_FROM_BROWSER` | no | Browser name for cookie loading (see below). |

You can keep several profiles and pick one per run:

```bash
juanita -c ~/configs/home-mealie.env https://youtu.be/VIDEO_ID
```

## Usage

Examples use the bare `juanita` command (prefix with `fades -d juanita-mealie -x
juanita --` or use the alias above if you didn't install it globally):

```bash
# One or more videos
juanita https://youtu.be/wUewR4C0I_Y https://youtu.be/rzL07v6w8AA

# A recipe webpage (anything yt-dlp doesn't recognize as a video falls back
# to a plain page fetch — title, text, and og:image thumbnail)
juanita https://hermanathome.com/lemongrass-chicken-in-crispy-rice-paper/

# A local recipe text file — any positional that's a file on disk is read as
# recipe notes instead of fetched as a URL. Videos, webpages, and files can
# all be mixed together.
juanita grandmas-walnut-bread.txt https://youtu.be/VIDEO_ID

# Preview the parsed recipe without writing to Mealie (no Mealie token needed)
juanita --dry-run https://youtu.be/UlafoXGyx6g

# Batch from a file (one URL per line, # comments allowed)
juanita --from-file urls.txt

# Skip tag creation/attachment
juanita --no-tags https://youtu.be/VIDEO_ID

# Store ingredients as plain text instead of linking them to your foods DB
juanita --not-linked-ingredients https://youtu.be/VIDEO_ID
```

Each source is handled independently — one failure doesn't stop the batch, and
the exit code is non-zero if any source failed. Use `-v` for full tracebacks,
`-q` to only show warnings and errors.

### Local text files

Pass a path to any text file containing a recipe (ingredients, steps — however
roughly noted) and it's sent straight to Claude; no URL, transcript, or cookies
involved. The recipe is written in the source's own language, and since there's
no source URL or thumbnail, those Mealie fields are simply left unset.

### Caption rate limits (HTTP 429)

YouTube sometimes rate-limits transcript (caption) downloads with `HTTP 429`.
Juanita retries with backoff, but a sustained 429 is best solved by passing
browser cookies so the request is authenticated:

```bash
juanita --cookies-from-browser firefox https://youtu.be/VIDEO_ID
# or a cookies.txt export:
juanita --cookies cookies.txt https://youtu.be/VIDEO_ID
# or set YTDLP_COOKIES_FROM_BROWSER in your config
```

If you don't have cookies handy, waiting a few minutes usually clears it.

## How it works

1. Each input becomes a common source record. For a **video URL**, `yt-dlp`
   extracts the title, description, thumbnail, and auto-generated transcript
   (no video download); for any other **URL**, the page's title, `og:image`,
   and visible text are scraped directly; for a **local file**, the text is
   read as-is.
2. **Claude** turns the source into a structured recipe (name, description,
   yield, instructions, tags, and ingredients split into quantity/unit/food/note)
   via structured outputs, in the source's own language.
3. **Mealie** gets a new recipe created and filled in via its REST API. Each
   ingredient's **food and unit are linked to your Mealie database** (looked up,
   or created if new), so amounts parse and ingredients aggregate in shopping
   lists instead of being plain text — pass `--not-linked-ingredients` to keep
   them as plain text and leave your database untouched. The source URL is set as
   `orgURL` and the thumbnail as the image, when available.

See [CLAUDE.md](./CLAUDE.md) for the detailed pipeline and design notes.

## Notes

- Recipes are created **directly via the live Mealie API**. Re-running the same
  source creates a duplicate (Mealie appends `-1`, `-2`, … to the slug).
- For videos, the YouTube thumbnail is used as the recipe image. Mealie can't
  scrape recipes straight from YouTube URLs, which is why this transcript→LLM
  route exists.
- For plain webpages, the image comes from the page's `og:image` (falling back
  to `twitter:image`) when the page sets one; sites without either get no image.

## Web frontend

For dropping URLs in from a browser instead of a terminal, `juanita-web` is a
small FastAPI app over the same pipeline: paste a URL, it runs as a background
job (extraction takes 30s+), and the page polls until it's done. It's gated by
a single shared secret (HTTP Basic auth — any username, the configured
password), not full user accounts, so only run it somewhere you'd trust with
the same access as Mealie itself.

### With Docker Compose

```bash
cp .env.example .env   # fill in ANTHROPIC_API_KEY, MEALIE_URL, MEALIE_TOKEN,
                        # and JUANITA_WEB_TOKEN (a password of your choosing)
docker compose up -d --build
```

Open `http://localhost:8000`, log in with `JUANITA_WEB_TOKEN` as the password.

### With plain Docker

```bash
docker build -t juanita-web .
docker run -p 8000:8000 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  -e MEALIE_URL=https://mealie.example.com \
  -e MEALIE_TOKEN=... \
  -e JUANITA_WEB_TOKEN=... \
  juanita-web
```

### Without Docker

```bash
pip install 'juanita-mealie[web]'
ANTHROPIC_API_KEY=... MEALIE_URL=... MEALIE_TOKEN=... JUANITA_WEB_TOKEN=... juanita-web
```

### On Kubernetes

Same image, wired up with a `Secret` for the four variables above. A minimal
example:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: juanita-web
stringData:
  ANTHROPIC_API_KEY: sk-ant-...
  MEALIE_URL: https://mealie.example.com
  MEALIE_TOKEN: ...
  JUANITA_WEB_TOKEN: ...
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: juanita-web
spec:
  replicas: 1 # job status lives in memory in the one process; don't scale out
  selector:
    matchLabels: { app: juanita-web }
  template:
    metadata:
      labels: { app: juanita-web }
    spec:
      containers:
        - name: juanita-web
          image: juanita-web # build & push the Dockerfile above to your registry
          ports: [{ containerPort: 8000 }]
          envFrom: [{ secretRef: { name: juanita-web } }]
---
apiVersion: v1
kind: Service
metadata:
  name: juanita-web
spec:
  selector: { app: juanita-web }
  ports: [{ port: 80, targetPort: 8000 }]
```

Point an Ingress (or your internal DNS) at the Service to land it at
`juanita.internal`.

### Notes for headless/container use

- `YTDLP_COOKIES_FROM_BROWSER` doesn't work here — there's no browser profile
  to read cookies from inside a container. If YouTube starts rate-limiting
  caption downloads (`HTTP 429`) from your cluster's IP, mount a `cookies.txt`
  export into the container and set `YTDLP_COOKIES_FILE` to its path instead.
- Job history is in-memory and capped at the last 50 imports; it resets on
  restart. Mealie remains the durable record of anything actually imported.

## Development

Run Juanita straight from your checkout with [fades](https://github.com/PyAr/fades)
— `bin/run_dev` installs this project editable in a managed venv (your changes
under `src/` are picked up on the next run) and forwards its arguments:

```bash
git clone https://github.com/gilgamezh/juanita-mealie
cd juanita-mealie
bin/run_dev --dry-run https://youtu.be/VIDEO_ID
bin/run_dev init
```

Added a new dependency to `pyproject.toml`? Rebuild the venv with
`FADES_REBUILD=1 bin/run_dev ...`.

Lint with ruff:

```bash
fades -d ruff -x ruff -- check src
```

## The name

[Juana "Juanita" Bordoy](https://es.wikipedia.org/wiki/Juanita_Bordoy)
(1916–1995) was the kitchen assistant of Doña Petrona C. de Gandulfo, Argentina's
most famous cook. From around 1945 until Doña Petrona's death in 1992, Juanita
did the unglamorous half of the work on live television — beating, cleaning,
mixing, measuring, prepping — almost always silent and in the background while
Doña Petrona presented. She became so emblematic that *"ser una Juanita"* entered
Argentine speech for the person doing the supporting, behind-the-scenes work.

Naming a tool after her is affectionate, but it shouldn't flatten the story: it's
also one of a celebrated *patrona* and the working-class domestic employee whose
labor made the show run yet was paid and recognized very differently. That
inequality is the point worth remembering — this tool is "the Juanita," doing the
prep so your Mealie library looks effortless.

If you want the real history, read Rebekah E. Pite, ["Entertaining Inequalities:
Doña Petrona, Juanita Bordoy, and Domestic Work in Mid-Twentieth-Century
Argentina,"](https://read.dukeupress.edu/hahr/article/91/1/97/70686/Entertaining-Inequalities-Dona-Petrona-Juanita)
*Hispanic American Historical Review* 91, no. 1 (2011): 97–128.

## License

[GPL-3.0-or-later](./LICENSE).
