# Coverarr

One place to manage posters, overlays and collections for **Plex, Jellyfin and Emby**, so
all three look the same. It's a Kometa alternative built around doing as little work as
possible: it tracks what each server already has and only touches what actually changed.

> **Status: Phase 4 (automation).** Coverarr connects to your servers, imports and matches
> everything, shows where the servers disagree, creates missing collections and fills in
> missing members, and pushes the base posters you choose, with overlays drawn on, to every
> server. Radarr/Sonarr webhooks keep new and upgraded titles up to date, a weekly scan
> catches anything missed, and collections built from TMDb/Trakt lists and charts are kept the
> same on every server. See [docs/ROADMAP.md](docs/ROADMAP.md) for what's next.

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
  - collections whose members differ → **add the missing members** (Coverarr never removes
    anything on its own; on a collection's page you can remove a title from it on every server)
  - posters that differ → **pick which server has the clean base poster**
  - titles missing on a server
  Anything can be ignored; ignored items stay ignored across imports.
- **Your own posters.** On any film, show or collection: **Upload poster**, or **Find on
  TMDb** to pick from TMDb's posters (English and textless first; needs a TMDb API key). The
  image is kept in the assets folder and becomes the base poster.
- **Poster sets** (Settings → Poster sets): import a zip or folder of posters named by title,
  such as a set you've downloaded from ThePosterDB: `Hot Fuzz (2007).jpg`,
  `Cornetto Collection.jpg`, `Spaced (1999) - Season 1.jpg`. ThePosterDB has no API and its
  terms forbid automated downloads, so download the set yourself and import the zip.
- **Reset posters from TMDb** (Settings → Reset posters): replaces base posters with TMDb's
  main English poster for every film, show, season and collection (or one library). By default
  your uploads and poster-set imports are kept; you can also limit it to titles without a base.
  Films need a TMDb id, shows are matched by TMDb, TVDB or IMDb id, collections by name.
  Nothing reaches your servers until you push from *Changes*; overlays are drawn on the new
  posters if switched on. Images are fingerprinted on every core but one, and a re-run skips
  titles that already have TMDb's current poster, so a cancelled reset carries on quickly.
- **Every core is used** for the heavy image work in every task: fingerprinting during scans and
  refreshes, Kometa and poster-set imports, storing bases, the TMDb reset and overlay renders all
  share one pool of worker processes (every core but one).
- **Radarr and Sonarr webhooks** (Settings → Automation). Add Coverarr's webhook URL in
  Radarr/Sonarr (*Settings → Connect → Webhook*, with On Import, On Upgrade, On Rename and the
  delete events). For each file event Coverarr:
  - asks every server to scan that title's folder (optional, if your servers don't already),
  - waits, then finds the title on each server by id, or among the most recently added, and
    checks that the server lists the new files; servers still scanning are tried again after 1,
    2, 5 and 15 minutes,
  - refreshes just that title (with its seasons and episodes), gives new titles and seasons
    TMDb's main poster as their base, fingerprints the posters and pushes that title, with
    overlays redrawn if, say, an upgrade made it 4K or HDR.

  A season pack's webhooks (one per episode) are combined into one update.
- **Weekly safety-net scan** (Settings → Automation): a full import, fingerprints, then a push
  of only the posters that really differ from their base. Pick the day and hour (the
  container's local time; set `TZ`). A scan missed while Coverarr was down runs when it's back;
  *Run the scan now* runs one straight away.
- **List collections** (sidebar → List collections): collections built from lists, made in
  Coverarr or imported from Kometa's YAML. Sources, merged when there are several:
  - TMDb lists, TMDb collections, single TMDb films/shows, and Trakt lists
  - charts: TMDb popular, top rated, trending (day/week), now playing, upcoming, airing today,
    on the air; Trakt trending, popular, anticipated, most watched this week, box office. You
    choose how many titles, e.g. the top 20.

  Each collection has a sync mode, *add only* or *exact* (exact removes titles that have left
  the lists, but never when a list fails to load), an order (release date, alphabetical or the
  list's own order; Plex keeps list order, Jellyfin/Emby sort those by name), and an optional
  sort title and summary. *Sync* creates the collection on every server that's missing it and
  adds the titles you have; a new collection from a TMDb collection gets TMDb's poster.
  Collections switched to *Keep in sync* sync daily (Settings → Automation) and in the weekly
  scan. Collections imported from Kometa start switched off, so nothing changes until you
  choose to take them over. Kometa files can use the chart builders too
  (e.g. `tmdb_popular: 30`). There's no IMDb chart: IMDb has no API (TMDb top rated or a
  Trakt list are good stand-ins).
- **Cancel** any queued or running task from *Activity* (or next to its progress bar). Work it
  already finished, such as posters already pushed, is kept.
- **A–Z bar** on films, TV and collections ("The"/"A"/"An" are skipped, so *The Holiday* is
  under H).
- **Overlays** (sidebar → Overlays). Upload an overlay pack once (e.g. the jmxd zip; it's
  unpacked into `/config/assets/overlays`), then switch overlays on per library. The default
  layers follow a typical Kometa jmxd setup: top and bottom gradients and a resolution badge
  (4K / 1080p… with DV, HDR, HDR10+) top-left on films, shows, seasons and episodes (episodes use
  the episode gradients), the network logo bottom-left on shows, and an optional edition banner.
  Every layer can be switched off, moved or added, with a live preview on any title.
  - Resolution, HDR, Dolby Vision and edition come from Plex (and its HDR/DV filters), Jellyfin's
    picture size, and the Radarr/Sonarr file name (`[Bluray-2160p][DV HDR10]`,
    `{edition-Director's Cut}`). Seasons and shows show their best episode.
  - The overlaid poster is what *Changes* pushes. Before the first overlaid push, the clean
    poster is kept as the title's **Original** artwork, so overlays are always drawn on a clean
    image and never stack (like Kometa's Original Posters).
  - Coverarr remembers exactly what it pushed. Only titles whose base, media info (e.g. a 4K
    upgrade) or overlay settings changed are rendered and pushed again. Rendering uses every
    CPU core but one; rendered posters are cached in `/config/cache/rendered`.
  - After updating to this version, run an **import** once so media info is collected.
- **Housekeeping** (sidebar → Housekeeping), for making Coverarr the source of truth:
  - *Without a base*: a suggested base for every film, show, season and collection that has
    none, with its reasons and a confidence (like the mismatch suggestions). It's the artwork
    most servers agree on, preferring a copy without overlays (Kometa's label, Coverarr's
    overlays, or top/bottom edges darker than just inside them). Accept one, pick another
    server's, or accept every high-confidence suggestion at once.
  - *Stored bases*: accepted bases (and any base still pointing at a server) are downloaded
    into the assets folder as the title's **Original** artwork, so a later change on the
    server can't change your base.
  - *Tidy stored posters*: Check, then Fix: misnamed or misplaced files, duplicate images for a
    title (the chosen one is kept), files nothing uses (old posters), records whose file is
    gone, empty folders.
  - *Force push*: upload every chosen poster to every server again, in case one is stale.
  - *Server clean-up*: Plex's Clean Bundles, Optimize Database and Empty Trash. Plex keeps
    every poster ever uploaded; Kometa's [ImageMaid](https://github.com/Kometa-Team/ImageMaid)
    removes the unused ones, since that needs Plex's data folder. Jellyfin and Emby replace the
    image, so nothing piles up.
- **Changes and Push.** Choosing a base poster (a server's poster or a Kometa original) only
  records the choice. The *Changes* page lists every title, season, episode and collection
  whose chosen poster isn't on every server yet, and why (different poster, still has
  Kometa's overlays, no poster). **Push all**, or push one, uploads the base to just those
  servers. On Plex the Kometa `Overlay` label is removed, since the new poster doesn't carry
  Kometa's overlays. A server that already shows the same artwork is skipped.
- **Poster fingerprints.** Each poster gets a perceptual hash of its middle band, so Kometa's
  top/bottom gradients and corner badges don't count as a difference, but different artwork
  does. Plex items Kometa has overlaid (its `Overlay` label) are flagged.
- **Incremental by design.** Each server's poster reference changes whenever the artwork
  changes, so re-scans only download posters that changed, and thumbnails are cached on disk.
  Every import is followed by a fingerprint scan of just the new or changed posters. A full
  import of ~3,800 movies, 700 shows, 28,000 episodes and 480 collections across three
  servers takes well under a minute (the first fingerprint scan takes a minute or two more).
- **Import from Kometa** (Settings → Import from Kometa):
  - *Original posters*: upload a zip of Kometa's `config/overlays/<Library> Original Posters`
    folders, or import them from a folder Coverarr can see (e.g. copy them into the config
    folder and import `/config/kometa`). Files are matched by Plex rating key and kept as clean
    base posters in the assets folder (see below).
  - *Collection files*: upload your Kometa collection YAML (plus `config.yml` to place them in
    the right library and bring over TMDb/Trakt keys). TMDb lists, TMDb collections and Trakt
    lists are understood; **Check** compares each list with your library and with the
    collection Kometa built on each server.
- **Refresh one thing instead of re-importing.** Every film, show and collection page has a
  *Refresh* button that re-reads just that title (a show brings its seasons and episodes) or
  collection from every server, re-fingerprints any poster that changed and updates the review
  list. It takes a second or two, instead of a full import.
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
    movies/Clerks (1994) {tmdb-2292}/poster-kometa.jpg, poster-upload.jpg, poster-tmdb.jpg,
                                     poster-set.jpg
    tv/Spaced (1999) {tvdb-75592}/poster-kometa.jpg, season01-kometa.jpg, s01e02-kometa.jpg
    collections/Cornetto Collection/poster-kometa.jpg
    movies/…/poster-original.jpg   the clean poster overlays are drawn on
    overlays/jmxd/                 the overlay pack
  cache/                           thumbnails and rendered posters; safe to delete
    rendered/                      posters with overlays (re-rendered when needed)
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

CI also runs `scripts/scan-unicode.py` (shared with PEMForge) on every push and pull request.
It blocks bidirectional controls, invisible characters and homoglyph letters that a diff review
can't see. Run it locally with `python3 scripts/scan-unicode.py --verbose`.

Layout:

```
backend/coverarr/clients/    Plex + Jellyfin/Emby API clients (one interface)
backend/coverarr/services/   importer, matching, review (discrepancies), posters, actions, jobs
backend/coverarr/api/        FastAPI routes
backend/migrations/          Alembic migrations (run automatically at startup)
frontend/src/                React + TypeScript UI
```
