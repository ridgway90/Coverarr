# Roadmap

The goal: one source of truth for posters, overlays and collections, pushed identically to
Plex, Jellyfin and Emby, doing only the work that a change actually requires.

## Phase 1: Foundation ✅ (this release)

- Server connections, three-way import, ID-based matching, library pairing
- Review screen: missing collections (create), collection member gaps (add), poster
  differences (choose base), missing titles
- Poster fingerprinting that ignores overlay bands, with incremental re-scans
- Background jobs with an activity log, single-admin auth, Docker image

## Phase 2: Posters

- [x] Clean base poster store in `/config/assets` (Coverarr keeps its own originals, so
  overlays never stack)
- [x] Import **Kometa's original posters** folder (`config/overlays/<Library> Original Posters`)
  as the base for Plex items that have Kometa overlays
- [x] Poster picker from TMDb (and the existing server art); **upload your own**, which pins
  that item and overrides automatic choices everywhere
- [x] **Poster sets**: import a zip or folder of posters named by title (e.g. a set downloaded
  from ThePosterDB) for films, shows, seasons and collections in one go
- [x] **Pending changes**: choices are kept server-side; the *Changes* page lists every
  server that doesn't show the chosen poster yet and pushes them all, or one at a time
- [x] Fingerprint-based sync: a server counts as done when its poster's fingerprint matches
  the base (and Kometa's overlays aren't on it); Coverarr records the poster ref it pushed

## Phase 3: Overlays

- [x] Overlay pack upload (e.g. jmxd) into `/config/assets/overlays/<pack>`
- [x] Default layers matching the Kometa setup:
  - jmxd gradients (top/bottom; episode variants)
  - resolution + HDR/DV/HDR10+ badge, top-left (`resolution/<key>.png`); edition banner
  - network logo, bottom-left of show posters
- [x] Applied at **show, season and episode** level for TV; movies get gradients + resolution
- [x] Live preview editor; layers per library and level
- [x] Overlay inputs (base, resolution, HDR, edition, network, overlay files) make up the
  render key, so a 1080p → 4K upgrade re-renders just that item
- [x] Clean originals kept before the first overlaid push, so overlays never stack
- [x] Rendering on every core but one

## Phase 4: Automation and custom collections

- [x] Sonarr/Radarr webhooks (import, upgrade, rename, delete) → process only that item and push
- [x] Weekly safety-net full scan that compares and pushes only real differences
- [x] Custom collections defined in the UI, sourced from **TMDb lists/collections/ids**,
  **Trakt list URLs** (several merged) and TMDb/Trakt charts (trending, popular, top rated…);
  sort title, summary, release/alphabetical/list ordering; sync mode (add-only or exact);
  daily sync
- [x] Collection posters with overrides (TMDb's poster for new TMDb collections); created
  everywhere automatically
- [x] Labels on collections and their titles (Kometa's `label` / `item_label`)
- IMDb charts (no API)

## Later / nice to have

- [x] Episode title cards (text layers: episode number and title, with any font)
- [x] Backgrounds (fanart) as managed artwork: chosen per title, pushed everywhere
- [x] Metadata edits (titles, sort titles, summaries, labels), pushed to every server
- MediUX / ThePosterDB set import
- Import an existing Kometa config
