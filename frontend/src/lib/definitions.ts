/** Collections built from lists (TMDb, Trakt, charts), made in Coverarr or imported from Kometa. */

export interface Builder {
  type: string;
  value: string;
}

export interface SyncOutcome {
  added: number;
  removed: number;
  created?: boolean;
  skipped?: string;
  error?: string;
}

export interface Definition {
  id: number;
  name: string;
  library_id: number | null;
  source: string;
  source_file: string | null;
  builders: Builder[];
  options: { sync_mode?: "append" | "sync"; collection_order?: "release" | "alpha" | "custom"; sort_title?: string; summary?: string; label?: string[] | string; item_label?: string[] | string } & Record<
    string,
    unknown
  >;
  unsupported: string[];
  /** The sync mode in effect: the chosen one, or the default for these sources. */
  sync_mode: "append" | "sync";
  enabled: boolean;
  checked_at: string | null;
  check_result: {
    total: number;
    in_library: number;
    missing_count: number;
    missing: { title: string; year: number | null }[];
    errors: string[];
    collection: { id: number; name: string; servers: Record<string, { in_collection: number; extra: number }> } | null;
  } | null;
  synced_at: string | null;
  sync_result: {
    total?: number;
    in_library?: number;
    missing_count?: number;
    missing?: { title: string; year: number | null }[];
    errors: string[];
    notes: string[];
    servers: Record<string, SyncOutcome>;
  } | null;
  collection: { id: number; name: string; servers: number[] } | null;
}

type Kind = "movie" | "show";

export interface SourceType {
  type: string;
  label: string;
  /** What goes in the value box. */
  hint: string;
  kinds: Kind[];
  /** Charts take a number of titles rather than an id. */
  chart?: boolean;
  /** A fixed set of values (e.g. which IMDb chart), shown as a menu. */
  choices?: { value: string; label: string; kinds: Kind[] }[];
}

export const IMDB_CHARTS = [
  { value: "top_movies", label: "Top 250 films", kinds: ["movie"] as Kind[] },
  { value: "popular_movies", label: "Most popular films", kinds: ["movie"] as Kind[] },
  { value: "top_english", label: "Top rated English-language films", kinds: ["movie"] as Kind[] },
  { value: "box_office", label: "Box office", kinds: ["movie"] as Kind[] },
  { value: "lowest_rated", label: "Lowest rated films", kinds: ["movie"] as Kind[] },
  { value: "top_shows", label: "Top 250 shows", kinds: ["show"] as Kind[] },
  { value: "popular_shows", label: "Most popular shows", kinds: ["show"] as Kind[] },
];

export const SOURCE_TYPES: SourceType[] = [
  { type: "tmdb_list", label: "TMDb list", hint: "List id or URL", kinds: ["movie", "show"] },
  { type: "tmdb_collection", label: "TMDb collection", hint: "Collection id or URL", kinds: ["movie"] },
  { type: "trakt_list", label: "Trakt list", hint: "https://trakt.tv/users/…/lists/…", kinds: ["movie", "show"] },
  { type: "tmdb_movie", label: "TMDb film", hint: "Film id or URL", kinds: ["movie"] },
  { type: "tmdb_show", label: "TMDb show", hint: "Show id or URL", kinds: ["show"] },
  { type: "tmdb_popular", label: "TMDb popular", hint: "How many titles", kinds: ["movie", "show"], chart: true },
  { type: "tmdb_top_rated", label: "TMDb top rated", hint: "How many titles", kinds: ["movie", "show"], chart: true },
  { type: "tmdb_trending_weekly", label: "TMDb trending this week", hint: "How many titles", kinds: ["movie", "show"], chart: true },
  { type: "tmdb_trending_daily", label: "TMDb trending today", hint: "How many titles", kinds: ["movie", "show"], chart: true },
  { type: "tmdb_now_playing", label: "TMDb now in cinemas", hint: "How many titles", kinds: ["movie"], chart: true },
  { type: "tmdb_upcoming", label: "TMDb upcoming", hint: "How many titles", kinds: ["movie"], chart: true },
  { type: "tmdb_airing_today", label: "TMDb airing today", hint: "How many titles", kinds: ["show"], chart: true },
  { type: "tmdb_on_the_air", label: "TMDb on the air", hint: "How many titles", kinds: ["show"], chart: true },
  { type: "trakt_trending", label: "Trakt trending", hint: "How many titles", kinds: ["movie", "show"], chart: true },
  { type: "trakt_popular", label: "Trakt popular", hint: "How many titles", kinds: ["movie", "show"], chart: true },
  { type: "trakt_anticipated", label: "Trakt anticipated", hint: "How many titles", kinds: ["movie", "show"], chart: true },
  { type: "trakt_watched_weekly", label: "Trakt most watched this week", hint: "How many titles", kinds: ["movie", "show"], chart: true },
  { type: "trakt_boxoffice", label: "Trakt box office", hint: "How many titles", kinds: ["movie"], chart: true },
  { type: "imdb_chart", label: "IMDb chart", hint: "Which chart", kinds: ["movie", "show"], choices: IMDB_CHARTS },
  { type: "imdb_list", label: "IMDb list", hint: "https://www.imdb.com/list/ls…", kinds: ["movie", "show"] },
  { type: "imdb_id", label: "IMDb title", hint: "tt… id or URL", kinds: ["movie", "show"] },
  { type: "mdblist_list", label: "MDBList list", hint: "https://mdblist.com/lists/…", kinds: ["movie", "show"] },
  { type: "tmdb_discover", label: "TMDb discover", hint: "with_genres=27&sort_by=popularity.desc&limit=50", kinds: ["movie", "show"] },
  { type: "anilist_popular", label: "AniList popular", hint: "How many titles", kinds: ["movie", "show"], chart: true },
  { type: "anilist_trending", label: "AniList trending", hint: "How many titles", kinds: ["movie", "show"], chart: true },
  { type: "anilist_top_rated", label: "AniList top rated", hint: "How many titles", kinds: ["movie", "show"], chart: true },
  { type: "anilist_id", label: "AniList anime", hint: "AniList id", kinds: ["movie", "show"] },
  { type: "library_filter", label: "From your library", hint: "genre=Comedy, decade=1990, studio=…", kinds: ["movie", "show"] },
];

export const SOURCE_BY_TYPE = new Map(SOURCE_TYPES.map((s) => [s.type, s]));

export const ORDER_LABELS = { release: "Release date", alpha: "Alphabetical", custom: "List order" } as const;
export const MODE_LABELS = { append: "Add only", sync: "Exact (remove titles that leave the lists)" } as const;

/** A readable label and, where there is one, a link for a list source. */
export function builderLink(b: Builder): { label: string; href?: string } {
  const source = SOURCE_BY_TYPE.get(b.type);
  if (source?.chart) return { label: `${source.label} (top ${b.value})` };
  if (b.type === "tmdb_list") return { label: `TMDb list ${b.value}`, href: `https://www.themoviedb.org/list/${b.value}` };
  if (b.type === "tmdb_collection") return { label: `TMDb collection ${b.value}`, href: `https://www.themoviedb.org/collection/${b.value}` };
  if (b.type === "tmdb_movie") return { label: `TMDb film ${b.value}`, href: `https://www.themoviedb.org/movie/${b.value}` };
  if (b.type === "tmdb_show") return { label: `TMDb show ${b.value}`, href: `https://www.themoviedb.org/tv/${b.value}` };
  if (b.type === "imdb_chart") {
    const chart = IMDB_CHARTS.find((c) => c.value === b.value);
    const path = { top_movies: "top", popular_movies: "moviemeter", top_english: "top-english-movies", box_office: "boxoffice", lowest_rated: "bottom", top_shows: "toptv", popular_shows: "tvmeter" }[b.value];
    return { label: `IMDb ${chart?.label.toLowerCase() ?? b.value}`, href: path ? `https://www.imdb.com/chart/${path}/` : undefined };
  }
  if (b.type === "imdb_list") return { label: `IMDb list ${b.value}`, href: `https://www.imdb.com/list/${b.value}/` };
  if (b.type === "imdb_id") return { label: `IMDb ${b.value}`, href: `https://www.imdb.com/title/${b.value}/` };
  if (b.type === "mdblist_list") return { label: `MDBList ${b.value}`, href: `https://mdblist.com/lists/${b.value}` };
  if (b.type === "tmdb_discover") return { label: `TMDb discover: ${decodeURIComponent(b.value).replace(/&/g, ", ")}` };
  if (b.type === "anilist_id") return { label: `AniList ${b.value}`, href: `https://anilist.co/anime/${b.value}` };
  if (b.type === "library_filter") return { label: `Library: ${b.value}` };
  if (b.type === "trakt_list") return { label: `Trakt ${b.value.replace(/^https?:\/\/(www\.)?trakt\.tv\//, "")}`, href: b.value };
  return { label: `${b.type}: ${b.value}` };
}
