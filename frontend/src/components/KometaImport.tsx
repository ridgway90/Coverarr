import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, ExternalLink, FileUp, FolderOpen, ListChecks, Trash2, Upload } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Job } from "../lib/api";
import { useLibraries, useServers } from "../lib/hooks";
import { Button, Card, ErrorNote, Field, PresenceChips, ServerChip, cx, formatDate, inputClass, selectClass } from "./ui";

interface Definition {
  id: number;
  name: string;
  library_id: number | null;
  source_file: string | null;
  builders: { type: string; value: string }[];
  options: Record<string, unknown>;
  unsupported: string[];
  checked_at: string | null;
  check_result: {
    total: number;
    in_library: number;
    missing_count: number;
    missing: { title: string; year: number | null }[];
    errors: string[];
    collection: { id: number; name: string; servers: Record<string, { in_collection: number; extra: number }> } | null;
  } | null;
  collection: { id: number; name: string; servers: number[] } | null;
}

interface YamlResult {
  saved: number;
  errors: string[];
  warnings: Record<string, string[]>;
  keys_imported: string[];
  unassigned: string[];
}

const KEY_LABELS: Record<string, string> = {
  tmdb_api_key: "TMDb API key",
  trakt_client_id: "Trakt client ID",
  trakt_client_secret: "Trakt client secret",
  trakt_access_token: "Trakt access token",
  trakt_refresh_token: "Trakt refresh token",
};

/** POST multipart with upload progress (fetch can't report it). */
function uploadWithProgress<T>(url: string, form: FormData, onProgress: (fraction: number) => void): Promise<T> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.withCredentials = true;
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded / e.total);
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* not JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body as T);
      else reject(new Error((body as { detail?: string })?.detail ?? `Upload failed (${xhr.status})`));
    };
    xhr.onerror = () => reject(new Error("Upload failed: connection lost"));
    xhr.send(form);
  });
}

function builderLink(b: { type: string; value: string }) {
  if (b.type === "tmdb_list") return { label: `TMDb list ${b.value}`, href: `https://www.themoviedb.org/list/${b.value}` };
  if (b.type === "tmdb_collection") return { label: `TMDb collection ${b.value}`, href: `https://www.themoviedb.org/collection/${b.value}` };
  const slug = b.value.replace(/^https?:\/\/(www\.)?trakt\.tv\//, "");
  return { label: `Trakt ${slug}`, href: b.value };
}

export default function KometaImport() {
  return (
    <div className="max-w-4xl space-y-6">
      <OriginalsSection />
      <YamlSection />
    </div>
  );
}

// --- original posters --------------------------------------------------------------------------

function OriginalsSection() {
  const qc = useQueryClient();
  const { data: servers } = useServers();
  const plex = (servers ?? []).filter((s) => s.kind === "plex");
  const [serverId, setServerId] = useState<number | "">("");
  const [progress, setProgress] = useState<number | null>(null);
  const [path, setPath] = useState("/config/kometa");
  const [error, setError] = useState<unknown>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const jobs = useQuery({
    queryKey: ["jobs", "kometa_originals"],
    queryFn: () => api.get<Job[]>("/api/jobs?limit=100"),
    select: (all) => all.filter((j) => j.kind === "kometa_originals"),
    refetchInterval: (q) => (q.state.data?.some((j) => j.kind === "kometa_originals" && ["queued", "running"].includes(j.status)) ? 1500 : false),
  });
  const latest = jobs.data?.[0];
  const chosenServer = serverId === "" ? (plex.length === 1 ? plex[0].id : null) : serverId;

  const afterStart = () => {
    qc.invalidateQueries({ queryKey: ["jobs"] });
    qc.invalidateQueries({ queryKey: ["dashboard"] });
  };

  const upload = async (file: File) => {
    setError(null);
    const form = new FormData();
    form.append("file", file);
    if (chosenServer) form.append("server_id", String(chosenServer));
    setProgress(0);
    try {
      await uploadWithProgress("/api/kometa/originals/upload", form, setProgress);
      afterStart();
    } catch (err) {
      setError(err);
    } finally {
      setProgress(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const fromFolder = useMutation({
    mutationFn: () => api.post("/api/kometa/originals/folder", { path, server_id: chosenServer }),
    onSuccess: afterStart,
  });

  return (
    <Card className="p-5">
      <h2 className="text-lg font-semibold">Original posters</h2>
      <p className="mt-1 text-sm text-muted">
        Kometa keeps the clean poster of everything it overlays in{" "}
        <code className="rounded bg-bg px-1">config/overlays/&lt;Library&gt; Original Posters</code>, named by Plex rating
        key. Coverarr imports them as clean base posters into{" "}
        <code className="rounded bg-bg px-1">/config/assets</code>, one folder per title, e.g.{" "}
        <code className="rounded bg-bg px-1">movies/Clerks (1994) {"{tmdb-2292}"}/poster-kometa.jpg</code>. They become the base
        wherever you haven't chosen one (or chose Plex's overlaid poster).
      </p>

      {plex.length > 1 && (
        <Field label="Plex server Kometa ran against">
          <select className={selectClass + " mt-1"} value={serverId} onChange={(e) => setServerId(e.target.value ? Number(e.target.value) : "")}>
            <option value="">Choose…</option>
            {plex.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </Field>
      )}
      {plex.length === 0 && <p className="mt-3 text-sm text-warn">Add your Plex server first; Kometa names originals by Plex rating key.</p>}

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div className="rounded-lg border border-line p-4">
          <div className="flex items-center gap-2 font-medium">
            <FileUp className="size-4" /> Upload a zip
          </div>
          <p className="mt-1 text-xs text-muted">Zip the "Original Posters" folder(s). Fine for smaller libraries.</p>
          <input
            ref={fileRef}
            type="file"
            accept=".zip"
            className="hidden"
            onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
          />
          <Button className="mt-3" icon={<Upload className="size-4" />} disabled={!chosenServer || progress !== null} onClick={() => fileRef.current?.click()}>
            Choose zip…
          </Button>
          {progress !== null && (
            <div className="mt-3">
              <div className="h-1.5 overflow-hidden rounded bg-line">
                <div className="h-full bg-accent transition-all" style={{ width: `${Math.round(progress * 100)}%` }} />
              </div>
              <div className="mt-1 text-xs text-muted">Uploading… {Math.round(progress * 100)}%</div>
            </div>
          )}
        </div>
        <div className="rounded-lg border border-line p-4">
          <div className="flex items-center gap-2 font-medium">
            <FolderOpen className="size-4" /> From a folder
          </div>
          <p className="mt-1 text-xs text-muted">
            Best for large libraries. Copy Kometa's <code>overlays</code> folder into Coverarr's config folder (e.g.{" "}
            <code>/opt/coverarr/config/kometa</code>), then import <code>/config/kometa</code>.
          </p>
          <div className="mt-3 flex gap-2">
            <input className={inputClass} value={path} onChange={(e) => setPath(e.target.value)} placeholder="/config/kometa" />
            <Button loading={fromFolder.isPending} disabled={!chosenServer || !path.trim()} onClick={() => fromFolder.mutate()}>
              Import
            </Button>
          </div>
        </div>
      </div>
      <div className="mt-3 space-y-2">
        <ErrorNote error={error ?? fromFolder.error} />
        {latest && <OriginalsResult job={latest} />}
      </div>
    </Card>
  );
}

function OriginalsResult({ job }: { job: Job }) {
  if (job.status === "queued" || job.status === "running") {
    return (
      <div className="rounded-md border border-line bg-panel-2 px-3 py-2 text-sm">
        Importing… {job.message ?? ""}
        <div className="mt-2 h-1 overflow-hidden rounded bg-line">
          <div className="h-full bg-accent" style={{ width: `${Math.round(job.progress * 100)}%` }} />
        </div>
      </div>
    );
  }
  if (job.status === "failed") {
    return <div className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">Import failed: {job.error}</div>;
  }
  const r = job.result ?? {};
  return (
    <div className="rounded-md border border-ok/30 bg-ok/10 px-3 py-2 text-sm">
      <div className="flex items-center gap-2 font-medium text-ok">
        <CheckCircle2 className="size-4" /> Imported {r.imported ?? 0} originals · {formatDate(job.finished_at)}
      </div>
      <div className="mt-1 text-muted">
        Used as the base for {r.applied ?? 0}; kept your own choice for {r.kept_choice ?? 0}.
        {r.unmatched ? ` ${r.unmatched} didn't match anything in Plex (removed or re-added titles).` : ""}
        {r.unreadable ? ` ${r.unreadable} unreadable.` : ""}
        {r.not_original ? ` ${r.not_original} other files ignored.` : ""}
      </div>
    </div>
  );
}

// --- collection YAML -----------------------------------------------------------------------------

function YamlSection() {
  const qc = useQueryClient();
  const { data: libs } = useLibraries();
  const { data: servers } = useServers();
  const serverMap = new Map((servers ?? []).map((s) => [s.id, s]));
  const [libraryId, setLibraryId] = useState("");
  const [importKeys, setImportKeys] = useState(true);
  const [result, setResult] = useState<YamlResult | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const definitions = useQuery({ queryKey: ["definitions"], queryFn: () => api.get<Definition[]>("/api/definitions") });
  const checking = useQuery({
    queryKey: ["jobs", "check_definitions"],
    queryFn: () => api.get<Job[]>("/api/jobs?limit=30"),
    select: (all) => all.find((j) => j.kind === "check_definitions" && ["queued", "running"].includes(j.status)),
    refetchInterval: (q) => (q.state.data?.some((j) => j.kind === "check_definitions" && ["queued", "running"].includes(j.status)) ? 1500 : false),
  });
  // Reload the results when a running check finishes.
  const wasChecking = useRef(false);
  useEffect(() => {
    if (wasChecking.current && !checking.data) qc.invalidateQueries({ queryKey: ["definitions"] });
    wasChecking.current = Boolean(checking.data);
  }, [checking.data, qc]);

  const upload = useMutation({
    mutationFn: async (files: FileList) => {
      const form = new FormData();
      for (const f of Array.from(files)) form.append("files", f);
      if (libraryId) form.append("library_id", libraryId);
      if (!importKeys) form.append("skip_keys", "true");
      const resp = await fetch("/api/kometa/yaml", { method: "POST", body: form, credentials: "same-origin" });
      const body = await resp.json();
      if (!resp.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Upload failed");
      return body as YamlResult;
    },
    onSuccess: (r) => {
      setResult(r);
      qc.invalidateQueries({ queryKey: ["definitions"] });
      qc.invalidateQueries({ queryKey: ["settings"] });
      if (fileRef.current) fileRef.current.value = "";
    },
  });
  const check = useMutation({
    mutationFn: (ids?: number[]) => api.post("/api/definitions/check", { ids }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["jobs"] }),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/api/definitions/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["definitions"] }),
  });
  const move = useMutation({
    mutationFn: ({ id, library_id }: { id: number; library_id: number | null }) => api.put(`/api/definitions/${id}`, { library_id }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["definitions"] }),
  });
  const libName = (id: number | null) => libs?.libraries.find((l) => l.id === id)?.name;

  return (
    <Card className="p-5">
      <h2 className="text-lg font-semibold">Collection files</h2>
      <p className="mt-1 text-sm text-muted">
        Upload your Kometa collection YAML files (e.g. <code>comfort-j.yml</code>, <code>christmas.yml</code>). Include{" "}
        <code>config.yml</code> to place each file in the right library and bring over your TMDb/Trakt keys. TMDb lists, TMDb
        collections and Trakt lists are understood, with sort title, order, label and summary. <strong>Check</strong> fetches
        each list and compares it with your library. Keeping these collections in sync automatically comes in a later update.
      </p>
      <div className="mt-4 flex flex-wrap items-end gap-4">
        <Field label="Library for files not listed in config.yml">
          <select className={selectClass + " mt-1"} value={libraryId} onChange={(e) => setLibraryId(e.target.value)}>
            <option value="">Automatic</option>
            {libs?.libraries.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name}
              </option>
            ))}
          </select>
        </Field>
        <label className="mb-2 flex items-center gap-2 text-sm">
          <input type="checkbox" checked={importKeys} onChange={(e) => setImportKeys(e.target.checked)} />
          Import TMDb/Trakt keys from config.yml
        </label>
        <input
          ref={fileRef}
          type="file"
          multiple
          accept=".yml,.yaml"
          className="hidden"
          onChange={(e) => e.target.files?.length && upload.mutate(e.target.files)}
        />
        <Button variant="primary" icon={<Upload className="size-4" />} loading={upload.isPending} onClick={() => fileRef.current?.click()}>
          Upload YAML files…
        </Button>
      </div>
      <div className="mt-3 space-y-2">
        <ErrorNote error={upload.error} />
        {result && (
          <div className="rounded-md border border-line bg-panel-2 px-3 py-2 text-sm">
            <div className="font-medium">Imported {result.saved} collection{result.saved === 1 ? "" : "s"}.</div>
            {result.keys_imported.length > 0 && (
              <div className="text-muted">Saved from config.yml: {result.keys_imported.map((k) => KEY_LABELS[k] ?? k).join(", ")}.</div>
            )}
            {result.unassigned.length > 0 && (
              <div className="text-warn">No library chosen for: {result.unassigned.join(", ")}. Pick one below.</div>
            )}
            {result.errors.map((e) => (
              <div key={e} className="text-danger">
                {e}
              </div>
            ))}
            {Object.entries(result.warnings).map(([name, ws]) => (
              <div key={name} className="text-warn">
                {name}: {ws.join("; ")}
              </div>
            ))}
          </div>
        )}
      </div>

      {(definitions.data?.length ?? 0) > 0 && (
        <>
          <div className="mt-6 flex items-center justify-between">
            <h3 className="font-semibold">Imported collections</h3>
            <Button size="sm" icon={<ListChecks className="size-4" />} loading={check.isPending || Boolean(checking.data)} onClick={() => check.mutate(undefined)}>
              Check all
            </Button>
          </div>
          {checking.data && <div className="mt-2 text-sm text-muted">{checking.data.message ?? "Checking…"}</div>}
          <ul className="mt-3 divide-y divide-line rounded-lg border border-line">
            {definitions.data!.map((d) => {
              const r = d.check_result;
              return (
                <li key={d.id} className="p-4">
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="font-medium">
                        {d.collection ? (
                          <Link to={`/collections/${d.collection.id}`} className="hover:text-accent">
                            {d.name}
                          </Link>
                        ) : (
                          d.name
                        )}
                        <span className="ml-2 text-xs font-normal text-muted">{d.source_file}</span>
                      </div>
                      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs">
                        {d.builders.map((b) => {
                          const link = builderLink(b);
                          return (
                            <a key={b.value} href={link.href} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent hover:underline">
                              {link.label} <ExternalLink className="size-3" />
                            </a>
                          );
                        })}
                      </div>
                      <div className="mt-1 text-xs text-muted">
                        {Object.entries(d.options)
                          .map(([k, v]) => `${k.replace(/_/g, " ")}: ${Array.isArray(v) ? v.join(", ") : String(v)}`)
                          .join(" · ")}
                      </div>
                      {d.unsupported.length > 0 && (
                        <div className="mt-1 flex items-center gap-1 text-xs text-warn">
                          <AlertTriangle className="size-3.5" /> Not supported yet: {d.unsupported.join(", ")}
                        </div>
                      )}
                      <div className="mt-1 flex items-center gap-2 text-xs text-muted">
                        {d.collection ? (
                          <>
                            Exists as a collection on <PresenceChips servers={serverMap} presentIds={d.collection.servers} expectedIds={d.collection.servers} />
                          </>
                        ) : (
                          "No matching collection on your servers yet"
                        )}
                      </div>
                    </div>
                    <div className="flex items-center gap-2">
                      <select
                        className={selectClass + " h-8"}
                        value={d.library_id ?? ""}
                        onChange={(e) => move.mutate({ id: d.id, library_id: e.target.value ? Number(e.target.value) : null })}
                        aria-label="Library"
                      >
                        <option value="">No library</option>
                        {libs?.libraries.map((l) => (
                          <option key={l.id} value={l.id}>
                            {l.name}
                          </option>
                        ))}
                      </select>
                      <Button size="sm" onClick={() => check.mutate([d.id])} disabled={!d.library_id}>
                        Check
                      </Button>
                      <Button size="sm" variant="ghost" icon={<Trash2 className="size-4" />} aria-label={`Remove ${d.name}`} onClick={() => confirm(`Remove "${d.name}" from Coverarr? Nothing changes on your servers.`) && remove.mutate(d.id)} />
                    </div>
                  </div>
                  {r && (
                    <div className={cx("mt-3 rounded-md border px-3 py-2 text-sm", r.errors.length ? "border-warn/40 bg-warn/5" : "border-line bg-panel-2")}>
                      <div>
                        <span className="font-medium">{r.total}</span> titles in the list ·{" "}
                        <span className="font-medium">{r.in_library}</span> in {libName(d.library_id) ?? "the library"}
                        {r.missing_count > 0 && <> · {r.missing_count} not in your library</>}
                        <span className="text-muted"> · checked {formatDate(d.checked_at)}</span>
                      </div>
                      {r.collection && (
                        <div className="mt-1 flex flex-wrap gap-3 text-xs text-muted">
                          {Object.entries(r.collection.servers).map(([sid, v]) => {
                            const s = serverMap.get(Number(sid));
                            return (
                              <span key={sid} className="inline-flex items-center gap-1">
                                {s && <ServerChip server={s} />} {v.in_collection}/{r.in_library} in the collection
                                {v.extra ? `, ${v.extra} not on the list` : ""}
                              </span>
                            );
                          })}
                        </div>
                      )}
                      {r.missing.length > 0 && (
                        <details className="mt-1 text-xs text-muted">
                          <summary className="cursor-pointer">Not in your library</summary>
                          {r.missing.map((m) => `${m.title}${m.year ? ` (${m.year})` : ""}`).join(", ")}
                          {r.missing_count > r.missing.length && ` and ${r.missing_count - r.missing.length} more`}
                        </details>
                      )}
                      {r.errors.map((e) => (
                        <div key={e} className="mt-1 text-xs text-warn">
                          {e}
                        </div>
                      ))}
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </>
      )}
    </Card>
  );
}
