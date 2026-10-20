export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const resp = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!resp.ok) {
    let message = resp.statusText;
    try {
      const data = await resp.json();
      if (typeof data.detail === "string") message = data.detail;
      else if (Array.isArray(data.detail)) message = data.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      /* not JSON */
    }
    if (resp.status === 401 && !path.startsWith("/api/auth")) {
      window.dispatchEvent(new Event("coverarr:unauthorized"));
    }
    throw new ApiError(resp.status, message);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T>(path: string, body?: unknown) => request<T>("PUT", path, body ?? {}),
  del: <T>(path: string) => request<T>("DELETE", path),
};

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") search.set(k, String(v));
  }
  const s = search.toString();
  return s ? `?${s}` : "";
}

// --- types --------------------------------------------------------------------------------------

export type ServerKind = "plex" | "jellyfin" | "emby";

export interface AuthStatus {
  setup_required: boolean;
  authenticated: boolean;
  username: string | null;
}

export interface Server {
  id: number;
  kind: ServerKind;
  name: string;
  url: string;
  has_token: boolean;
  verify_ssl: boolean;
  enabled: boolean;
  version: string | null;
  last_ok_at: string | null;
  last_error: string | null;
}

export interface TestResult {
  ok: boolean;
  error?: string;
  name?: string;
  version?: string;
  libraries?: { id: string; name: string; kind: string }[];
}

export interface ServerLibrary {
  id: number;
  server_id: number;
  server_name: string;
  server_kind: ServerKind;
  name: string;
  kind: "movie" | "show";
  enabled: boolean;
  library_id: number | null;
}

export interface Library {
  id: number;
  name: string;
  kind: "movie" | "show";
  item_count: number;
  server_libraries: ServerLibrary[];
}

export interface Presence {
  server_id: number;
  server_item_id?: number;
  server_collection_id?: number;
  has_poster: boolean;
  poster_version: string;
  kometa_overlay: boolean;
}

/** A clean image Coverarr holds itself (e.g. a Kometa original). */
export interface Artwork {
  artwork_id: number;
  server_id: null;
  source: string;
  has_poster: boolean;
  poster_version: string;
  kometa_overlay: boolean;
  width: number | null;
  height: number | null;
}

export type PosterRef = Presence | Artwork;

export interface ItemSummary {
  id: number;
  kind: string;
  title: string;
  year: number | null;
  poster_source: string | null;
  servers: Presence[];
  poster: PosterRef | null;
  issues: string[];
}

export interface Page<T> {
  total: number;
  page: number;
  page_size: number;
  items: T[];
}

export interface Issue {
  id: number;
  type: IssueType;
  status: "open" | "ignored";
  title: string;
  library_id: number | null;
  media_item_id: number | null;
  collection_id: number | null;
  details: Record<string, any>;
  updated_at: string;
}

export type IssueType =
  | "mismatch"
  | "similar_collections"
  | "item_missing" | "poster_differs" | "collection_missing" | "collection_members_differ";

export interface Season {
  id: number;
  title: string;
  season_number: number;
  servers: Presence[];
  episodes: Record<string, number>;
}

export interface ItemDetail extends Omit<ItemSummary, "issues"> {
  library_id: number;
  tmdb_id: string | null;
  tvdb_id: string | null;
  imdb_id: string | null;
  seasons: Season[];
  artwork: Artwork[];
  collections: { id: number; name: string }[];
  issues: Issue[];
}

export interface CollectionServer extends Presence {
  server_collection_id: number;
  remote_id: string;
  name: string;
  smart: boolean;
  member_count: number;
  matched_members: number;
}

export interface CollectionSummary {
  id: number;
  library_id: number | null;
  name: string;
  sort_title: string | null;
  poster_source: string | null;
  servers: CollectionServer[];
  poster: CollectionServer | Artwork | null;
  issues: string[];
}

export interface CollectionMember {
  id: number;
  title: string;
  year: number | null;
  in_collection_on: number[];
  servers: Presence[];
  poster: PosterRef | null;
}

export interface CollectionDetail extends Omit<CollectionSummary, "issues"> {
  summary: string | null;
  artwork: Artwork[];
  members: CollectionMember[];
  issues: Issue[];
}

export interface Job {
  id: number;
  kind: string;
  status: "queued" | "running" | "succeeded" | "failed";
  progress: number;
  message: string | null;
  params: Record<string, unknown>;
  state?: { servers?: Record<string, "waiting" | "importing" | "done" | "failed"> };
  result: Record<string, any> | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  log?: string[];
}

export interface Dashboard {
  servers: (Pick<Server, "id" | "name" | "kind" | "enabled" | "version" | "last_ok_at" | "last_error"> & {
    items: number;
  })[];
  counts: Record<"movie" | "show" | "season" | "episode" | "collection", number>;
  issues: Partial<Record<IssueType, number>>;
  last_import: Job | null;
  active_jobs: Job[];
}

export type ReviewSummary = Record<IssueType, { open: number; ignored: number }>;

export function posterUrl(p: PosterRef | null | undefined, width = 300): string | null {
  if (!p || !p.has_poster) return null;
  if ("artwork_id" in p) {
    return `/api/images/artwork/${p.artwork_id}?w=${width}&v=${encodeURIComponent(p.poster_version)}`;
  }
  if (p.server_collection_id !== undefined && p.server_item_id === undefined) {
    return `/api/images/collection/${p.server_collection_id}?w=${width}&v=${encodeURIComponent(p.poster_version)}`;
  }
  return `/api/images/item/${p.server_item_id}?w=${width}&v=${encodeURIComponent(p.poster_version)}`;
}
