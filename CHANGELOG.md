# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [1.0.0]

First stable release. The application is feature-complete, security-audited and
running in production behind a reverse proxy.

### Added

**Setup and configuration**
- First-run setup wizard: geocode an area by postcode or place name, set the
  centre and radius on an interactive map, and generate a boundary polygon
- Branding step for site name, tagline, theme and logo upload
- Quick links editor with per-link icon upload
- Admin settings page to change any of the above after setup

**Data sources**
- OpenStreetMap via Overpass, with a fallback mirror and a compact query that
  classifies categories in Python rather than in the query
- Food Standards Agency hygiene ratings, matched against existing places to
  enrich rather than duplicate
- NaPTAN public transport stops from the Department for Transport
- Overture Maps open place data, queried directly from S3 Parquet via DuckDB
  with automatic release detection
- Foursquare Places, using targeted category sweeps for coverage
- Google Places, with a field mask deliberately scoped to the Pro tier
- Background job runner with status polling, so slow imports can't hit a
  reverse-proxy timeout
- Staleness warnings when a source hasn't been refreshed in 30 days

**Community features**
- Submissions for new businesses, edits, closure reports, reviews and events
- Moderation queue with approve, reject and delete
- Reviews shown in place popups; approved events plotted as map pins
- Email and Pushover notifications on new submissions

**Duplicate management**
- Detection across sources using name normalisation and proximity clustering
- Per-record selection so individual matches can be excluded before merging
- Merging keeps the richest record, backfills missing fields, combines source
  attribution and re-points attached submissions

**Theming**
- 16 themes, each defining a full palette — primary, accent, surface, canvas,
  ink, muted and border — so every surface shifts together
- Five sidebar treatments derived from the primary colour
- Ten font choices loaded from Google Fonts
- Configurable map tiles with optional CARTO key

**Social links**
- Facebook, Instagram, X and email as square icon buttons beside quick links

### Security

- Static file serving restricted to `public/`; source, database and secrets are
  not web-accessible
- API keys stored in `data/secrets.json` with `0600` permissions, masked in the
  admin UI and never sent to the browser
- Setup endpoints lock permanently once setup is finalised, preventing
  unauthenticated imports against metered APIs
- Admin auth uses timing-safe comparison with a 5-attempt, 15-minute lockout
- Submission validation: star ratings bounded, fields capped at 2,000
  characters, event coordinates checked against the served area, `place_id`
  verified against the database
- Rate limiting at 10 submissions per IP per hour, plus a honeypot field
- CSP, HSTS, `X-Frame-Options`, `X-Content-Type-Options` and `Referrer-Policy`
  set on every response
- `upgrade-insecure-requests` sent only when the request arrived over HTTPS, so
  the app works over plain HTTP on a LAN
- Server refuses to start with a weak or missing `ADMIN_TOKEN`
- Runs as a non-root user in the container

### Accessibility

- Visible focus rings via `:focus-visible`, adapting to light and dark surfaces
- Every form control associated with a label; control groups use `radiogroup`
  and `aria-describedby` where a single label doesn't apply
- Modals declare `role="dialog"` with `aria-modal` and are labelled by heading
- Status and toast regions are `aria-live`, so updates are announced
- Skip link and `main` / `nav` landmarks
- `prefers-reduced-motion` honoured throughout
- Touch targets meet the 44px minimum, with expanded hit areas where the visual
  size is deliberately smaller
- All interface icons are SVG rather than emoji, so they render identically
  across platforms and inherit the theme colour
- Every theme verified to meet WCAG AA on text, surfaces and interface states

### Fixed

- Uploaded logos and quick-link icons persisted to the data volume instead of
  the image, so they survive container rebuilds
- Places with no name hidden from the map and excluded from category counts
- Import errors returned as JSON rather than HTML error pages
- DuckDB extension loading corrected, which had been failing silently
- Overture release detection replaces a hardcoded value that went stale monthly
- Category chip contrast derived from sidebar luminance so all themes stay
  readable

### Notes on upgrading

This is the first tagged release; there is no earlier version to upgrade from.
API keys previously held in `config.json` are migrated to `secrets.json`
automatically on first start.
