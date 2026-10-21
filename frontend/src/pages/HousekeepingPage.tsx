import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Layers, Search, Upload, Wrench } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import CancelJob from "../components/CancelJob";
import {
  Button,
  Card,
  ErrorNote,
  PageHeader,
  Pagination,
  Poster,
  ServerChip,
  Spinner,
  Tabs,
  cx,
  inputClass,
  selectClass,
  useClampPage,
  useDebounced,
} from "../components/ui";
import { api, qs, type Job, type PosterRef } from "../lib/api";
import { useJobAction, useLibraries, useServerMap, useServers } from "../lib/hooks";

type Confidence = "high" | "medium" | "low" | "none";

interface Candidate {
  server_id: number;
  server: string | null;
  poster: PosterRef | null;
  group: number;
  overlaid: boolean;
  edge_darkness: number | null;
}

interface Suggestion {
  target: "item" | "collection";
  id: number;
  title: string;
  kind: string;
  library: string | null;
  top_id: number | null;
  suggested_server_id: number | null;
  confidence: Confidence;
  reasons: string[];
  candidates: Candidate[];
}

interface BasesPage {
  total: number;
  counts: Record<Confidence, number>;
  page: number;
  page_size: number;
  items: Suggestion[];
}

interface Coverage {
  stored: number;
  server: number;
  none: number;
}

const CONFIDENCE_STYLE: Record<Confidence, string> = {
  high: "bg-ok/20 text-ok",
  medium: "bg-accent/20 text-accent",
  low: "bg-warn/20 text-warn",
  none: "bg-danger/20 text-danger",
};
const KIND_LABEL: Record<string, string> = { movie: "Film", show: "Show", season: "Season", collection: "Collection" };

const REFRESH = ["housekeeping", "changes", "items", "item", "collections", "collection", "issues", "review", "dashboard"];

export default function HousekeepingPage() {
  return (
    <>
      <PageHeader
        title="Housekeeping"
        subtitle="Make Coverarr the source of truth: a stored base poster for everything, a tidy assets folder, and servers that match."
      />
      <div className="space-y-6">
        <CoverageCard />
        <BasesCard />
        <TidyCard />
        <ForcePushCard />
        <ServersCard />
      </div>
    </>
  );
}

function JobNote({ job, describe }: { job: Job | undefined; describe: (r: Record<string, unknown>) => string }) {
  if (!job) return null;
  if (job.status === "queued" || job.status === "running") {
    return (
      <div className="mt-3 rounded-md border border-line bg-panel-2 px-3 py-2 text-sm">
        <div className="flex items-center justify-between gap-4">
          {job.message ?? "Queued"}
          <CancelJob job={job} />
        </div>
        <div className="mt-2 h-1 overflow-hidden rounded bg-line">
          <div className="h-full bg-accent transition-all" style={{ width: `${Math.round(job.progress * 100)}%` }} />
        </div>
      </div>
    );
  }
  if (job.status === "cancelled") return <div className="mt-3 rounded-md border border-line bg-panel-2 px-3 py-2 text-sm">Cancelled. Anything done before that is kept.</div>;
  if (job.status === "failed") return <div className="mt-3 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">{job.error}</div>;
  return <div className="mt-3 rounded-md border border-ok/30 bg-ok/10 px-3 py-2 text-sm">{describe(job.result ?? {})}</div>;
}

function CoverageCard() {
  const { data } = useQuery({
    queryKey: ["housekeeping", "summary"],
    queryFn: () => api.get<{ coverage: Record<string, Coverage>; without_base: Record<Confidence, number> }>("/api/housekeeping/summary"),
  });
  const store = useJobAction(() => api.post<{ job_id: number }>("/api/housekeeping/store-bases"), REFRESH);
  const serverOnly = Object.values(data?.coverage ?? {}).reduce((n, c) => n + c.server, 0);
  return (
    <Card className="p-5">
      <h2 className="font-semibold">Base posters</h2>
      <p className="mt-1 max-w-3xl text-sm text-muted">
        A <em>stored</em> base is a copy Coverarr keeps in <code>/config/assets</code>, so it doesn't change when a server's poster does.
        A <em>server</em> base points at whatever that server shows now.
      </p>
      {!data ? (
        <Spinner />
      ) : (
        <table className="mt-4 w-full max-w-xl text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-muted">
            <tr>
              <th className="py-1 font-medium"></th>
              <th className="py-1 font-medium">Stored</th>
              <th className="py-1 font-medium">Server only</th>
              <th className="py-1 font-medium">No base</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(data.coverage).map(([label, c]) => (
              <tr key={label} className="border-t border-line">
                <td className="py-1.5 capitalize">{label}</td>
                <td className="py-1.5 text-ok">{c.stored.toLocaleString()}</td>
                <td className={cx("py-1.5", c.server ? "text-accent" : "text-muted")}>{c.server.toLocaleString()}</td>
                <td className={cx("py-1.5", c.none ? "text-warn" : "text-muted")}>{c.none.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button icon={<CheckCircle2 className="size-4" />} loading={store.running} disabled={!serverOnly} onClick={() => store.run(undefined)}>
          Store all server bases{serverOnly ? ` (${serverOnly.toLocaleString()})` : ""}
        </Button>
        <span className="text-xs text-muted">Downloads each one from its server as the title's "Original" artwork.</span>
      </div>
      <ErrorNote error={store.error} />
      <JobNote job={store.job} describe={(r) => `Stored ${Number(r.stored ?? 0).toLocaleString()} bases${r.failed ? `; ${r.failed} failed (see Activity)` : "."}`} />
    </Card>
  );
}

function BasesCard() {
  const qc = useQueryClient();
  const { data: libs } = useLibraries();
  const servers = useServerMap();
  const [libraryId, setLibraryId] = useState("");
  const [kind, setKind] = useState("");
  const [confidence, setConfidence] = useState<"" | Confidence>("");
  const [search, setSearch] = useState("");
  const q = useDebounced(search);
  const [page, setPage] = useState(1);
  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ["housekeeping", "bases", libraryId, kind, confidence, q, page],
    queryFn: () => api.get<BasesPage>(`/api/housekeeping/bases${qs({ library_id: libraryId, kind, confidence, q, page, page_size: 30 })}`),
    placeholderData: keepPreviousData,
  });
  useClampPage(page, data, setPage);
  const refresh = () => {
    for (const key of REFRESH) qc.invalidateQueries({ queryKey: [key] });
  };
  const accept = useJobAction(
    (body: Record<string, unknown>) => api.post<{ job_id: number }>("/api/housekeeping/bases/accept", body),
    REFRESH,
  );
  const acceptOne = useMutation({
    mutationFn: (choice: { target: string; id: number; server_id: number }) => api.post("/api/housekeeping/bases/accept", { choices: [choice] }),
    onSuccess: refresh,
  });
  const high = data?.counts.high ?? 0;
  const bulk = () => {
    if (confirm(`Use the suggested poster as the base for ${high.toLocaleString()} high-confidence titles? Each is stored in the assets folder.`)) {
      accept.run({ confidence: "high", library_id: libraryId ? Number(libraryId) : undefined, kind: kind || undefined });
    }
  };

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="max-w-3xl">
          <h2 className="font-semibold">Without a base</h2>
          <p className="mt-1 text-sm text-muted">
            Coverarr's best guess for each, like the mismatch suggestions: the artwork most servers agree on, preferring a copy without
            overlays (Kometa's label, or darker gradient edges). Check them, pick a different server's poster if needed, then accept.
          </p>
        </div>
        <Button variant="primary" icon={<CheckCircle2 className="size-4" />} loading={accept.running} disabled={!high} onClick={bulk}>
          Accept all high confidence{high ? ` (${high.toLocaleString()})` : ""}
        </Button>
      </div>
      <ErrorNote error={error ?? accept.error ?? acceptOne.error} />
      <JobNote job={accept.job} describe={(r) => `Stored ${Number(r.stored ?? 0).toLocaleString()} bases. Push them from Changes.`} />

      <div className="mt-4 flex flex-wrap gap-3">
        <div className="relative min-w-52 flex-1 sm:max-w-xs">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <input className={inputClass + " pl-9"} placeholder="Search" value={search} onChange={(e) => (setSearch(e.target.value), setPage(1))} />
        </div>
        <select className={selectClass} value={libraryId} onChange={(e) => (setLibraryId(e.target.value), setPage(1))}>
          <option value="">All libraries</option>
          {(libs?.libraries ?? []).map((l) => (
            <option key={l.id} value={l.id}>
              {l.name}
            </option>
          ))}
        </select>
        <select className={selectClass} value={kind} onChange={(e) => (setKind(e.target.value), setPage(1))}>
          <option value="">Everything</option>
          <option value="movie">Films</option>
          <option value="show">Shows</option>
          <option value="season">Seasons</option>
          <option value="collection">Collections</option>
        </select>
      </div>
      <div className="mt-3">
        <Tabs<"" | Confidence>
          value={confidence}
          onChange={(c) => (setConfidence(c), setPage(1))}
          tabs={[
            { id: "", label: "All" },
            ...(["high", "medium", "low", "none"] as Confidence[]).map((c) => ({
              id: c,
              label: `${c === "none" ? "No guess" : c[0].toUpperCase() + c.slice(1)} (${(data?.counts[c] ?? 0).toLocaleString()})`,
            })),
          ]}
        />
      </div>

      {isLoading ? (
        <Spinner />
      ) : !data?.items.length ? (
        <p className="py-8 text-center text-sm text-muted">Everything here has a base poster.</p>
      ) : (
        <div className={isFetching ? "opacity-70 transition-opacity" : ""}>
          <ul className="divide-y divide-line">
            {data.items.map((s) => (
              <li key={`${s.target}-${s.id}`} className="flex flex-wrap gap-4 py-4">
                <div className="min-w-56 flex-1">
                  <Link to={s.target === "collection" ? `/collections/${s.id}` : `/items/${s.id}`} className="font-medium hover:text-accent">
                    {s.title}
                  </Link>
                  <div className="mt-0.5 flex items-center gap-2 text-xs text-muted">
                    <span className={cx("rounded px-1.5 py-0.5 font-semibold", CONFIDENCE_STYLE[s.confidence])}>
                      {s.confidence === "none" ? "no guess" : s.confidence}
                    </span>
                    {KIND_LABEL[s.kind] ?? s.kind}
                    {s.library ? ` · ${s.library}` : ""}
                  </div>
                  <ul className="mt-2 space-y-0.5 text-xs text-muted">
                    {s.reasons.map((r) => (
                      <li key={r}>{r}</li>
                    ))}
                  </ul>
                </div>
                <div className="flex flex-wrap gap-3">
                  {s.candidates.map((c) => {
                    const server = servers.get(c.server_id);
                    const suggested = c.server_id === s.suggested_server_id;
                    return (
                      <div key={c.server_id} className={cx("w-24 rounded-md border p-1.5", suggested ? "border-accent bg-accent/5" : "border-line")}>
                        <Poster presence={c.poster} alt={`${s.title}: ${c.server}`} width={200} />
                        <div className="mt-1 flex items-center justify-between gap-1">
                          {server && <ServerChip server={server} />}
                          {c.overlaid && (
                            <span title="Looks like it has overlays" className="text-warn">
                              <Layers className="size-3.5" />
                            </span>
                          )}
                        </div>
                        <Button
                          size="sm"
                          variant={suggested ? "primary" : "secondary"}
                          className="mt-1 w-full px-1 text-xs"
                          disabled={!c.poster || acceptOne.isPending}
                          onClick={() => acceptOne.mutate({ target: s.target, id: s.id, server_id: c.server_id })}
                        >
                          {suggested ? "Accept" : "Use"}
                        </Button>
                      </div>
                    );
                  })}
                </div>
              </li>
            ))}
          </ul>
          <Pagination page={page} pageSize={data.page_size} total={data.total} onPage={setPage} />
        </div>
      )}
    </Card>
  );
}

function TidyCard() {
  const check = useJobAction((apply: boolean) => api.post<{ job_id: number }>("/api/housekeeping/tidy", { apply }), REFRESH);
  const result = check.job?.result as { applied?: boolean; counts?: Record<string, number>; examples?: Record<string, string[]> } | undefined;
  const labels: Record<string, string> = {
    missing_files: "records whose file is missing",
    duplicates: "duplicate images",
    renamed: "files with the wrong name or folder",
    unused_files: "files nothing uses (old posters)",
  };
  const found = Object.values(result?.counts ?? {}).reduce((a, b) => a + b, 0);
  return (
    <Card className="p-5">
      <h2 className="flex items-center gap-2 font-semibold">
        <Wrench className="size-4" /> Tidy stored posters
      </h2>
      <p className="mt-1 max-w-3xl text-sm text-muted">
        Checks <code>/config/assets</code>: every file named and placed as expected, no duplicate images for a title (the chosen one is kept), no
        old posters nothing uses, no records pointing at missing files. <strong>Check</strong> only reports; <strong>Fix</strong> makes the
        changes. The overlay pack isn't touched.
      </p>
      <div className="mt-3 flex gap-2">
        <Button loading={check.running} onClick={() => check.run(false)}>
          Check
        </Button>
        <Button
          variant="primary"
          disabled={check.running}
          onClick={() => confirm("Fix the stored posters? Unused files and duplicates are deleted.") && check.run(true)}
        >
          Fix
        </Button>
      </div>
      <ErrorNote error={check.error} />
      {check.job && check.job.status !== "succeeded" && <JobNote job={check.job} describe={() => ""} />}
      {check.job?.status === "succeeded" && result && (
        <div className="mt-3 rounded-md border border-line bg-panel-2 px-3 py-2 text-sm">
          {found === 0 ? (
            <span className="text-ok">All tidy.</span>
          ) : (
            <>
              <div className="font-medium">{result.applied ? "Fixed" : "Found"}:</div>
              <ul className="mt-1 space-y-1">
                {Object.entries(result.counts ?? {}).map(([key, n]) => (
                  <li key={key}>
                    <details>
                      <summary className="cursor-pointer">
                        {n.toLocaleString()} {labels[key] ?? key}
                      </summary>
                      <ul className="mt-1 max-h-40 overflow-y-auto text-xs text-muted">
                        {(result.examples?.[key] ?? []).map((e) => (
                          <li key={e}>{e}</li>
                        ))}
                      </ul>
                    </details>
                  </li>
                ))}
              </ul>
              {!result.applied && <div className="mt-2 text-xs text-muted">Nothing has been changed yet: press Fix.</div>}
            </>
          )}
        </div>
      )}
    </Card>
  );
}

function ForcePushCard() {
  const { data: libs } = useLibraries();
  const [libraryId, setLibraryId] = useState("");
  const push = useJobAction(
    () => api.post<{ job_id: number }>("/api/changes/push", { force: true, library_id: libraryId ? Number(libraryId) : undefined }),
    REFRESH,
  );
  return (
    <Card className="p-5">
      <h2 className="flex items-center gap-2 font-semibold">
        <Upload className="size-4" /> Force push
      </h2>
      <p className="mt-1 max-w-3xl text-sm text-muted">
        Uploads every chosen base (or overlaid poster) to every server again, even where it already looks right. Use it if you suspect a
        server is showing something stale. Titles without a base aren't touched. Plex keeps every poster ever uploaded, so this adds to
        its metadata folder; see below.
      </p>
      <div className="mt-3 flex flex-wrap gap-2">
        <select className={selectClass} value={libraryId} onChange={(e) => setLibraryId(e.target.value)}>
          <option value="">All libraries</option>
          {(libs?.libraries ?? []).map((l) => (
            <option key={l.id} value={l.id}>
              {l.name}
            </option>
          ))}
        </select>
        <Button
          icon={<Upload className="size-4" />}
          loading={push.running}
          onClick={() => confirm("Upload every chosen poster to every server again?") && push.run(undefined)}
        >
          Force push
        </Button>
      </div>
      <ErrorNote error={push.error} />
      <JobNote
        job={push.job}
        describe={(r) => `Uploaded ${Number(r.uploaded ?? 0).toLocaleString()} posters${r.failed ? `; ${r.failed} failed (see Activity)` : "."}`}
      />
    </Card>
  );
}

function ServersCard() {
  const { data: servers } = useServers();
  const plex = (servers ?? []).filter((s) => s.kind === "plex");
  const run = useMutation({
    mutationFn: ({ id, action }: { id: number; action: string }) => api.post<{ message: string }>(`/api/housekeeping/servers/${id}/maintenance`, { action }),
  });
  return (
    <Card className="p-5">
      <h2 className="font-semibold">Server clean-up</h2>
      <div className="mt-1 max-w-3xl space-y-2 text-sm text-muted">
        <p>
          <strong className="text-text">Plex</strong> keeps every poster ever uploaded or downloaded for an item in its metadata folder,
          so pushes add up over time.
        </p>
        <ul className="list-inside list-disc">
          <li>
            <em>Clean Bundles</em> deletes the metadata (artwork included) of items no longer in your libraries.
          </li>
          <li>
            <em>Optimize Database</em> and <em>Empty Trash</em> tidy the rest.
          </li>
          <li>
            Old posters of items that are still there, uploaded but no longer selected, aren't removed by Plex itself. Kometa's{" "}
            <a className="text-accent hover:underline" href="https://github.com/Kometa-Team/ImageMaid" target="_blank" rel="noreferrer">
              ImageMaid
            </a>{" "}
            deletes those. It needs access to Plex's data folder, so run it next to Plex.
          </li>
        </ul>
        <p>
          <strong className="text-text">Jellyfin and Emby</strong> replace the image when a poster is pushed (and with "save artwork into
          media folders", overwrite <code>poster.jpg</code>), so nothing piles up. Their own scheduled tasks (Dashboard → Scheduled Tasks)
          clean caches.
        </p>
      </div>
      {plex.length === 0 ? (
        <p className="mt-3 text-sm text-muted">No Plex server added.</p>
      ) : (
        plex.map((s) => (
          <div key={s.id} className="mt-3 flex flex-wrap items-center gap-2">
            <ServerChip server={s} />
            <span className="mr-2 text-sm font-medium">{s.name}</span>
            {[
              ["clean_bundles", "Clean Bundles"],
              ["optimize", "Optimize Database"],
              ["empty_trash", "Empty Trash"],
            ].map(([action, label]) => (
              <Button key={action} size="sm" disabled={run.isPending} onClick={() => run.mutate({ id: s.id, action })}>
                {label}
              </Button>
            ))}
          </div>
        ))
      )}
      <ErrorNote error={run.error} />
      {run.data && <p className="mt-2 text-sm text-ok">{run.data.message}. Plex carries on in the background.</p>}
    </Card>
  );
}
