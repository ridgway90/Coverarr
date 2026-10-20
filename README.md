# Coverarr

One place to manage posters, overlays and collections for **Plex, Jellyfin and Emby**, so
all three look the same. It's a Kometa alternative built around doing as little work as
possible: it tracks what each server already has and only touches what actually changed.

> **Status: Phase 1 (foundation).** Coverarr connects to your servers, imports and matches
> everything, shows where the servers disagree, and can create missing collections and fill
> in missing members. Poster pushing, overlays and *arr webhooks come next; see
> [docs/ROADMAP.md](docs/ROADMAP.md).

## What Phase 1 does

- **Connects to Plex, Jellyfin and Emby** with a connection test that lists the libraries it found.
- **Imports all three** (movies, shows, seasons, episodes, collections and their members) and
  **matches them by TMDb/TVDB/IMDb id**. Titles with no ids fall back to title and year.
  Libraries are paired automatically (e.g. Plex "Films" ↔ Jellyfin "Movies"); you can change
  pairings in *Settings → Library mapping*.
- **Mismatch detection.** Your servers share the same files, so if one file is "9" on
  Jellyfin but "9 Songs" on Plex, one of them is wrong. Coverarr lists these with a best guess
  and its reasons (IDs in the file name, how many servers agree, title/year) and re-matches
  the wrong server through its own "fix match" once you confirm. You can also enter any
  TMDb/TVDB id yourself. If your choice contradicts the file name, it reminds you to fix
  Radarr/Sonarr too.
- **Review screen** listing every difference between servers:
  - collections missing on a server → **create them there** in one click (or all at once)
  - collections whose members differ → **add the missing members** (nothing is ever removed)
  - posters that differ → **pick which server has the clean base poster**
  - titles missing on a server
  Anything can be ignored; ignored items stay ignored across imports.
- **Poster fingerprints.** Each poster gets a perceptual hash of its middle band, so Kometa's
  top/bottom gradients and corner badges don't count as a difference, but different artwork
  does. Plex items Kometa has overlaid (its `Overlay` label) are flagged.
- **Incremental by design.** Each server's poster reference changes whenever the artwork
  changes, so re-scans only download posters that changed, and thumbnails are cached on disk.
  A full import of ~3,800 movies, 700 shows, 28,000 episodes and 480 collections across three
  servers takes well under a minute.
- **Import from Kometa** (Settings → Import from Kometa):
  - *Original posters*: upload a zip of Kometa's `config/overlays/<Library> Original Posters`
    folders, or import them from a folder Coverarr can see (e.g. copy them into the config
    folder and import `/config/kometa`). Files are matched by Plex rating key and kept as clean
    base posters in the assets folder (see below).
  - *Collection files*: upload your Kometa collection YAML (plus `config.yml` to place them in
    the right library and bring over TMDb/Trakt keys). TMDb lists, TMDb collections and Trakt
    lists are understood; **Check** compares each list with your library and with the
    collection Kometa built on each server.
- **Server load setting** (gentle / normal / fast). Normal imports all servers at once;
  gentle goes one at a time with fewer parallel requests.
- Single admin login, activity log for every import, scan and change pushed to a server.

## Install (Docker, e.g. a Debian LXC on Proxmox)

1. Create a Debian 12 LXC (2 GB RAM and 8 GB disk is plenty). In the container's
   *Options → Features* enable **nesting** (and **keyctl** for unprivileged containers), then
   install Docker:
   ```sh
   apt update && apt install -y curl
   curl -fsSL https://get.docker.com | sh
   ```
2. Copy [`docker-compose.yml`](docker-compose.yml) to e.g. `/opt/coverarr/` and start it:
   ```sh
   mkdir -p /opt/coverarr/config && chown 1000:1000 /opt/coverarr/config
   cd /opt/coverarr && docker compose up -d
   ```
   The image is published to `ghcr.io/ridgway90/coverarr` by GitHub Actions on every push to
   `main`. To build it yourself instead, clone the repo and use `build: .` in the compose file.
3. Open `http://<lxc-ip>:2683`, create the admin account, then:
   - **Settings → Media servers**: add Plex (URL + X-Plex-Token), Jellyfin and Emby
     (URL + API key from each server's dashboard). Use **Test** to check each one.
   - **Dashboard → Import from servers**, then **Scan posters**.
   - Work through **Review**.

All state lives in `/config`, so back up that folder:

```
/config/
  coverarr.db
  assets/                          artwork Coverarr keeps (back this up)
    movies/Clerks (1994) {tmdb-2292}/poster-kometa.jpg
    tv/Spaced (1999) {tvdb-75592}/poster-kometa.jpg, season01-kometa.jpg, s01e02-kometa.jpg
    collections/Cornetto Collection/poster-kometa.jpg
    overlays/                      overlay images (coming with overlays)
  cache/                           thumbnails and rendered posters; safe to delete
```

## Development

```sh
# backend (Python 3.12+, uses uv)
cd backend
uv sync
uv run pytest
COVERARR_CONFIG_DIR=./dev-config uv run uvicorn coverarr.main:app --reload --port 2683

# fake Plex/Jellyfin/Emby servers with sample data, for trying the UI without real servers
uv run python -m tests.fakeserver   # Plex  http://127.0.0.1:32499/plex      token plex-token
                                    # Jelly http://127.0.0.1:32499/jellyfin  key   jf-key
                                    # Emby  http://127.0.0.1:32499/embysrv   key   emby-key

# frontend (proxies /api to :2683)
cd frontend
npm install
npm run dev
```

Layout:

```
backend/coverarr/clients/    Plex + Jellyfin/Emby API clients (one interface)
backend/coverarr/services/   importer, matching, review (discrepancies), posters, actions, jobs
backend/coverarr/api/        FastAPI routes
backend/migrations/          Alembic migrations (run automatically at startup)
frontend/src/                React + TypeScript UI
```
