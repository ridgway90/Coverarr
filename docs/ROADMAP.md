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
- [ ] Poster picker from TMDb (and the existing server art); **upload your own**, which pins
  that item and overrides automatic choices everywhere
- [x] **Pending changes**: choices are kept server-side; the *Changes* page lists every
  server that doesn't show the chosen poster yet and pushes them all, or one at a time
- [x] Fingerprint-based sync: a server counts as done when its poster's fingerprint matches
  the base (and Kometa's overlays aren't on it); Coverarr records the poster ref it pushed

## Phase 3: Overlays

- Overlay templates matching the current Kometa setup:
  - jmxd gradients (top/bottom; episode variants)
  - resolution + HDR/DV + edition badge, top-left (`resolution/<key><alt>.png`)
  - network logo, bottom-left of show posters
- Applied at **show, season and episode** level for TV; movies get gradients + resolution
- Live preview editor; rules per library
- Overlay inputs (resolution, HDR, edition, network) are part of the fingerprint, so a
  1080p → 4K upgrade re-renders just that item

## Phase 4: Automation and custom collections

- Sonarr/Radarr webhooks (import, upgrade, rename, delete) → process only that item and push
- Weekly safety-net full scan that compares and pushes only real differences
- Custom collections defined in the UI, sourced from **TMDb list ids**, **Trakt list URLs**
  (several merged), and later IMDb Top 250 / trending; sort title, summary, label,
  release-date ordering; sync mode (add-only or exact)
- Collection posters with overrides; create everywhere automatically

## Later / nice to have

- Backgrounds, season posters and episode title cards as managed artwork
- Metadata edits (titles, sort titles, summaries, labels)
- MediUX / ThePosterDB set import
- Import an existing Kometa config
