# Local Map

**Version 1.0.0** · [Changelog](CHANGELOG.md)

A self-hosted community map for any UK town, village or neighbourhood. Point it
at an area, import local data from open sources, and you have a searchable map of
shops, food, services, GPs, schools, parks and transport — with a moderation
queue so residents can submit corrections, reviews and events.

Built as a single container that can be configured for any area — deploy one
instance per town and each keeps its own data, branding and settings.

---

## What it does

- **Interactive map** with category filters, smart search, clustering and food
  hygiene ratings
- **First-run setup wizard** — pick an area by postcode, set branding, import data
- **Six data sources**, all wired into the wizard and admin panel
- **Community submissions** — residents can add businesses, report closures,
  leave reviews and submit events, all queued for moderation
- **Duplicate detection** with per-record selection before merging
- **16 themes**, each a full palette, plus configurable fonts and map tiles
- **Admin panel** for moderation, theming, quick links and re-running imports
- **Email and push notifications** when something needs reviewing
- **Accessible by default** — keyboard navigable, screen-reader labelled,
  WCAG AA contrast across every theme

Runs as a single Docker container with a SQLite database. No external services
required beyond the data sources you choose to enable.

---

## Quick start

```bash
git clone <your-repo-url> localmap
cd localmap

# Generate an admin token — the server refuses to start without a strong one
python3 -c "import secrets; print(secrets.token_urlsafe(32))"

cp .env.example .env
nano .env          # paste the token as ADMIN_TOKEN

mkdir -p data && sudo chown 1000:1000 data

docker compose up -d --build
docker compose logs -f
```

Open `http://<host>:8001` and the setup wizard runs. The startup log prints your
randomised admin URL — note it down, it isn't guessable by design.

> **Create `data/` before the first run.** If Docker creates it, it will be owned
> by root and the container (running as UID 1000) can't write the database.

---

## Setup wizard

Six steps, all reversible from the admin panel afterwards.

| Step | What it does |
|---|---|
| **Area** | Enter a postcode or place name. Geocodes via postcodes.io with a Nominatim fallback, drops a draggable marker, and generates a boundary from your chosen radius (1–10km) |
| **Branding** | Site name, tagline, theme colour, logo upload |
| **Quick links** | External links shown in the map's Quick Links popup |
| **Data import** | Run any of the six sources. OSM first — the others enrich what it finds |
| **Review** | Confirm everything before committing |
| **Done** | Writes config and opens the map |

---

## Data sources

Run OSM first to build the base layer. The rest match against existing places
within 300m by name similarity and fill in gaps rather than creating duplicates.

| Source | Key needed | What it adds |
|---|---|---|
| **OpenStreetMap** | No | Base layer — shops, food, schools, GPs, parks, services |
| **Food Standards Agency** | No | Hygiene ratings for every inspected food business, plus venues OSM missed |
| **NaPTAN** | No | Official bus stops, rail and tram stations from the DfT |
| **Overture Maps** | No | Open place data from Meta, Microsoft, Amazon and TomTom, queried via DuckDB |
| **Foursquare** | Yes (free) | Business listings with phone numbers and websites |
| **Google Places** | Yes (billing) | The best opening hours and phone numbers available |

All six are re-runnable from **Settings → Data sources**. Imports run in the
background with progress polling, so a slow source can't hit a reverse-proxy
timeout. The admin panel warns when a source hasn't been refreshed in 30 days.

### Google Places cost

Google bills each call at the highest SKU tier of *any* field requested. The
field mask here deliberately excludes `rating`, `reviews` and `photos`, which
would push calls from Pro to Enterprise tier and roughly double the cost.

One import run makes **12 API calls** against a free allowance of 5,000 Pro-tier
calls per month. Set a daily quota cap in the Cloud Console anyway — there is no
hard limit by default.

### Overture

Queries Overture's public Parquet files on S3 through DuckDB with a bounding-box
filter, so it reads only the relevant row groups rather than downloading a
release. The first run takes a minute or so while DuckDB fetches metadata. If the
pinned release has been retired, set a current one in Settings — they're listed
at [docs.overturemaps.org](https://docs.overturemaps.org).

---

## Duplicate detection

Importing from several sources leaves the same business listed more than once —
Overture draws partly from OpenStreetMap, and Foursquare and Google overlap with
both. **Settings → Duplicate places** finds records with similar names close
together and folds them into one.

Matching normalises names first, stripping punctuation, company suffixes and
leading articles, then requires both name similarity and physical proximity. The
record holding the most detail is kept, gaps on it are backfilled from its
duplicates, source attribution is combined, and any reviews or reports attached
to a duplicate are re-pointed at the keeper.

Scanning is non-destructive. Results are listed with each record's source,
distance and field count so you can judge borderline matches, and every entry has
a checkbox — untick anything that isn't really a duplicate. Detection re-runs
server-side on merge, so a stale browser tab can't act on outdated data.

Nothing is deleted. Duplicates are marked `merged`, which removes them from the
map but leaves them recoverable with a single SQL update.

---

## Accessibility

The interface is built to WCAG 2.1 AA:

- Keyboard navigable throughout, with visible `:focus-visible` rings that adapt
  to light and dark surfaces
- Every form control has an associated label; control groups use appropriate
  ARIA roles
- Modals are proper dialogs; status messages and toasts are announced through
  live regions
- Skip link and landmark regions for screen reader navigation
- `prefers-reduced-motion` honoured — animations are disabled when the operating
  system asks
- Touch targets meet the 44px minimum
- Interface icons are SVG, not emoji, so they render identically everywhere and
  inherit the theme colour
- Active states are never signalled by colour alone

---

## Configuration

Everything area-specific lives in `data/config.json`, generated by the wizard and
editable from the admin panel. API keys are stored separately in
`data/secrets.json` with `0600` permissions and are never sent to the browser.

### Environment variables

| Variable | Required | Purpose |
|---|---|---|
| `ADMIN_TOKEN` | **Yes** | Admin API auth. Minimum 20 characters or the server won't start |
| `ADMIN_PATH` | No | Admin URL path. Randomised each start if unset |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `ADMIN_EMAIL` | No | Email notifications |
| `PUSHOVER_TOKEN` / `PUSHOVER_USER` | No | Push notifications |
| `DATA_DIR` | No | Data directory, defaults to `data` |

API keys are **not** set here — enter them in the wizard or admin Settings so
they go to the secrets store rather than your environment.

---

## Theming

Sixteen presets, each defining a complete palette — primary, accent, surface,
canvas, text, muted text and border — so the page background, form fields,
popups and borders all shift together rather than only the sidebar changing:

Midnight · Forest · Harbour · Ocean · Claret · Heather · Terracotta · Charcoal ·
Sage · Slate · Plum · Moss · Denim · Cocoa · Indigo · Rust

Pick one and adjust individual values if you want. The sidebar background offers
five treatments (gradient, diagonal, solid, deep, accent mix), all derived from
the primary colour. Ten fonts load from Google Fonts automatically.

Every theme has been checked against WCAG AA for text on surfaces, white on the
primary colour, and the category list in the sidebar.

### Map tiles

| Layer | Key needed |
|---|---|
| Light (Esri grey canvas) | No |
| Light (CARTO) | Yes — free key from carto.com |
| Standard (OpenStreetMap) | No |
| Satellite (Esri) | No |

Without a CARTO key their tiles carry an "API KEY REQUIRED" watermark, so the
default falls back to Esri's grey canvas, which looks similar and needs nothing.

---

## Deployment behind a reverse proxy

The container serves plain HTTP on 8001. Put it behind nginx, Caddy, Traefik or
NGINX Proxy Manager for TLS.

`ProxyFix` is enabled for one hop, so rate limiting and brute-force lockout see
real client IPs rather than the proxy's. Forward these headers:

```nginx
proxy_set_header X-Real-IP $remote_addr;
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
client_max_body_size 2m;
add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
```

`upgrade-insecure-requests` is only sent in the CSP when the request actually
arrived over HTTPS, so the app works over plain HTTP on a LAN without breaking
same-origin `fetch()` calls.

### Restricting the admin panel

The randomised admin path deters scanning but isn't authentication. If your
proxy sits behind Cloudflare, a WAF rule adds a real second layer:

```
(http.request.uri.path eq "/<your-admin-path>" and ip.src ne <your.ip.here>)
```

Action: Block. Use Managed Challenge instead if your IP is dynamic.

---

## Security

- Only `public/` is web-accessible — source, database and secrets are not served
- API keys stored outside `config.json`, masked in the admin UI, never sent to
  the browser in full
- Admin auth uses timing-safe comparison with a 5-attempt, 15-minute lockout
- Submissions rate-limited to 10 per IP per hour, with a honeypot field
- All input validated: star ratings bounded, fields capped at 2,000 characters,
  event coordinates checked against the served area, `place_id` verified
- Setup endpoints lock permanently once setup completes, so the import routes
  can't be triggered anonymously against metered APIs
- Server refuses to start with a weak or missing `ADMIN_TOKEN`
- All SQL parameterised; the one dynamic column name goes through a whitelist
- CSP, HSTS, `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy` set
- Container runs as a non-root user

---

## Project layout

```
localmap/
├── server.py              Flask backend — API, importers, admin, setup
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── CHANGELOG.md
├── public/                The only web-accessible directory
│   ├── index.html         Map frontend
│   ├── setup.html         First-run wizard
│   ├── admin.html         Moderation and settings
│   ├── style.css
│   └── logo.png
└── data/                  Volume mount — not in the image
    ├── config.json        Site configuration
    ├── secrets.json       API keys, 0600
    ├── places.db          SQLite database
    └── uploads/           Uploaded logos and icons
```

Uploads live in `data/` rather than `public/` so they survive image rebuilds.

---

## Common tasks

```bash
docker compose logs -f                    # follow logs
docker compose up -d --build              # rebuild after changes
docker compose down                       # stop (data is safe in ./data)

sqlite3 data/places.db "SELECT COUNT(*) FROM places WHERE status='active';"
sqlite3 data/places.db "SELECT group_name, COUNT(*) FROM places WHERE status='active' GROUP BY group_name;"
```

### Running without Docker

```bash
pip install -r requirements.txt
mkdir -p data
export ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python3 server.py            # optional port argument, defaults to 8001
                             # serves via waitress when installed
```

---

## Deploying a second area

Each instance covers one area. Copy the project, use a separate `data/`
directory and a different port:

```yaml
services:
  arnold:
    build: .
    ports:
      - "8002:8001"
    volumes:
      - ./data-arnold:/app/data
    environment:
      ADMIN_TOKEN: "${ARNOLD_ADMIN_TOKEN:?required}"
```

Run the wizard again for the new area. The codebase is identical; only `data/`
differs.

---

## Data licensing

OpenStreetMap data is ODbL — attribution is rendered on the map. Overture is
permissively licensed. FSA and NaPTAN are Open Government Licence. Google Places
and Foursquare have their own terms restricting caching and redistribution;
review them before publishing a map built primarily on those sources.

---

## Licence

MIT — see `LICENSE`.
