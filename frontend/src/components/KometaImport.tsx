import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, FileUp, FolderOpen, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, uploadWithProgress, type Job } from "../lib/api";
import { useLibraries, useServers } from "../lib/hooks";
import { Button, Card, ErrorNote, Field, formatDate, inputClass, selectClass } from "./ui";

interface YamlResult {
  saved: number;
  errors: string[];
  warnings: Record<string, string[]>;
  keys_imported: string[];
  unassigned: string[];
  jobs: number[];
  defaults: Record<string, { collections: number; unsupported: string[]; franchise: boolean; notes?: string[] }>;
  overlays: Record<string, { applied: string[]; unsupported: string[]; kept_existing: boolean }>;
  metadata_files: Record<string, string[]>;
  asset_directories: string[];
  unknown_libraries: string[];
}

const KEY_LABELS: Record<string, string> = {
  tmdb_api_key: "TMDb API key",
  mdblist_api_key: "MDBList API key",
  trakt_client_id: "Trakt client ID",
  trakt_client_secret: "Trakt client secret",
  trakt_access_token: "Trakt access token",
  trakt_refresh_token: "Trakt refresh token",
};

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
  const [libraryId, setLibraryId] = useState("");
  const [importKeys, setImportKeys] = useState(true);
  const [result, setResult] = useState<YamlResult | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

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

  return (
    <Card className="p-5">
      <h2 className="text-lg font-semibold">Collection files</h2>
      <p className="mt-1 text-sm text-muted">
        Upload your Kometa collection YAML files (e.g. <code>comfort-j.yml</code>, <code>christmas.yml</code>). Include{" "}
        <code>config.yml</code> to place each file in the right library, bring over your TMDb/Trakt/MDBList keys, and turn
        Kometa's <code>- default:</code> collections and overlays into Coverarr ones. Templates, TMDb lists, collections,
        charts and discover searches, Trakt and IMDb lists and charts, MDBList lists, AniList charts and{" "}
        <code>plex_search</code> are understood, with sort title, order, sync mode and summary.
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
          Import TMDb/Trakt/MDBList keys from config.yml
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
              <div className="text-warn">No library chosen for: {result.unassigned.join(", ")}. Pick one on the List collections page.</div>
            )}
            <ConfigReport result={result} />
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

      <p className="mt-4 text-sm">
        Kometa metadata files (titles, sort titles, summaries, labels and <code>url_poster</code>/<code>url_background</code>)
        go in under Poster sets → <em>Kometa YAML</em>, and an <code>asset_directory</code> (<code>Title (Year)/poster.jpg</code>,{" "}
        <code>background.jpg</code>, <code>Season01.jpg</code>, <code>S01E01.jpg</code>) imports as a poster set.
      </p>
      <p className="mt-2 text-sm">
        Imported collections appear on the{" "}
        <Link to="/lists" className="text-accent hover:underline">
          List collections
        </Link>{" "}
        page, switched off: check them there, then switch on <em>Keep in sync</em> for the ones Coverarr should take over from Kometa.
      </p>
    </Card>
  );
}

function ConfigReport({ result }: { result: YamlResult }) {
  const defaults = Object.entries(result.defaults ?? {});
  const overlays = Object.entries(result.overlays ?? {});
  const metadata = Object.entries(result.metadata_files ?? {});
  return (
    <>
      {defaults.map(([lib, d]) => (
        <div key={`d-${lib}`} className="text-muted">
          {lib}: {d.collections} default collection{d.collections === 1 ? "" : "s"}
          {d.franchise ? "; franchise collections are being looked up on TMDb (see Jobs)" : ""}
          {d.unsupported.length > 0 && <span className="text-warn"> · not supported: {d.unsupported.join(", ")}</span>}
          {(d.notes ?? []).map((n) => (
            <div key={n} className="text-warn">
              {n}
            </div>
          ))}
        </div>
      ))}
      {overlays.map(([lib, o]) => (
        <div key={`o-${lib}`} className="text-muted">
          {lib} overlays:{" "}
          {o.kept_existing
            ? "kept the overlay settings you already have in Coverarr"
            : o.applied.length
              ? `turned on ${o.applied.join(", ")} (overlays stay off until you enable them on the Overlays page)`
              : "nothing Coverarr can draw"}
          {o.unsupported.length > 0 && <span className="text-warn"> · not supported: {o.unsupported.join(", ")}</span>}
        </div>
      ))}
      {metadata.map(([lib, files]) => (
        <div key={`m-${lib}`} className="text-muted">
          {lib} metadata files: {files.join(", ")}. Import them under Poster sets → Kometa YAML.
        </div>
      ))}
      {(result.asset_directories ?? []).length > 0 && (
        <div className="text-muted">
          Asset folder{result.asset_directories.length === 1 ? "" : "s"}: {result.asset_directories.join(", ")}. Copy into Coverarr's config
          folder and import as a poster set.
        </div>
      )}
      {(result.unknown_libraries ?? []).length > 0 && (
        <div className="text-warn">No Coverarr library named: {result.unknown_libraries.join(", ")}.</div>
      )}
    </>
  );
}
