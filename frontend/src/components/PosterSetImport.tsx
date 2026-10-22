import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, FileCode, FileUp, FolderOpen, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { api, uploadWithProgress, type Job } from "../lib/api";
import { Button, Card, ErrorNote, formatDate, inputClass } from "./ui";

const SET_JOBS = ["poster_set", "url_artwork"];

/** Import a set of posters: a zip, a folder, or MediUX/Kometa YAML with image links. */
export default function PosterSetImport() {
  const qc = useQueryClient();
  const [progress, setProgress] = useState<number | null>(null);
  const [path, setPath] = useState("/config/sets");
  const [keepChoices, setKeepChoices] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const jobs = useQuery({
    queryKey: ["jobs", "poster_set"],
    queryFn: () => api.get<Job[]>("/api/jobs?limit=100"),
    select: (all) => all.filter((j) => SET_JOBS.includes(j.kind)),
    refetchInterval: (q) => (q.state.data?.some((j) => SET_JOBS.includes(j.kind) && ["queued", "running"].includes(j.status)) ? 1500 : false),
  });
  const latest = jobs.data?.[0];

  const afterStart = () => {
    for (const key of ["jobs", "dashboard"]) qc.invalidateQueries({ queryKey: [key] });
  };

  const upload = async (file: File) => {
    setError(null);
    const form = new FormData();
    form.append("file", file);
    form.append("keep_choices", String(keepChoices));
    setProgress(0);
    try {
      await uploadWithProgress("/api/poster-sets/upload", form, setProgress);
      afterStart();
    } catch (err) {
      setError(err);
    } finally {
      setProgress(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const fromFolder = useMutation({
    mutationFn: () => api.post("/api/poster-sets/folder", { path, keep_choices: keepChoices }),
    onSuccess: afterStart,
  });
  const [yamlText, setYamlText] = useState("");
  const fromYaml = useMutation({
    mutationFn: () => api.post("/api/poster-sets/yaml", { yaml: yamlText, keep_choices: keepChoices }),
    onSuccess: () => {
      setYamlText("");
      afterStart();
    },
  });

  return (
    <Card className="p-5">
      <h2 className="font-semibold">Poster sets</h2>
      <p className="mt-1 max-w-3xl text-sm text-muted">
        Import a whole set of posters at once, such as a set you've downloaded from{" "}
        <a className="text-accent hover:underline" href="https://theposterdb.com" target="_blank" rel="noreferrer">
          ThePosterDB
        </a>{" "}
        (sign in there and use <em>Download set</em>). Files are matched to your library by name:
      </p>
      <ul className="mt-2 list-inside list-disc text-sm text-muted">
        <li>
          <code>Hot Fuzz (2007).jpg</code>: a film or show
        </li>
        <li>
          <code>Cornetto Collection.jpg</code>: a collection
        </li>
        <li>
          <code>Spaced (1999) - Season 1.jpg</code>, <code>Spaced (1999) - Specials.jpg</code>: a season
        </li>
        <li>
          <code>{"{tmdb-4638}"}</code> or <code>{"{tvdb-75592}"}</code> in a name pins it to that id
        </li>
      </ul>
      <p className="mt-2 text-sm text-muted">
        Each poster is saved under <code>/config/assets</code> and becomes the base poster, ready to push under{" "}
        <em>Changes</em>.
      </p>
      <label className="mt-3 flex items-center gap-2 text-sm">
        <input type="checkbox" checked={keepChoices} onChange={(e) => setKeepChoices(e.target.checked)} />
        Keep base posters I've already chosen (only fill in titles without one)
      </label>

      <div className="mt-4 grid gap-4 md:grid-cols-2">
        <div className="rounded-lg border border-line p-4">
          <div className="flex items-center gap-2 font-medium">
            <FileUp className="size-4" /> Upload a zip
          </div>
          <p className="mt-1 text-xs text-muted">The set's zip as downloaded, or any zip of posters named as above.</p>
          <input
            ref={fileRef}
            type="file"
            accept=".zip"
            className="hidden"
            onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
          />
          <Button className="mt-3" icon={<Upload className="size-4" />} disabled={progress !== null} onClick={() => fileRef.current?.click()}>
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
            A folder Coverarr can see, e.g. copy sets into <code>/opt/coverarr/config/sets</code> and import{" "}
            <code>/config/sets</code>. Subfolders are included.
          </p>
          <div className="mt-3 flex gap-2">
            <input className={inputClass} value={path} onChange={(e) => setPath(e.target.value)} placeholder="/config/sets" />
            <Button loading={fromFolder.isPending} disabled={!path.trim()} onClick={() => fromFolder.mutate()}>
              Import
            </Button>
          </div>
        </div>
      </div>
      <div className="mt-4">
        <div className="rounded-lg border border-line p-4">
          <div className="flex items-center gap-2 font-medium">
            <FileCode className="size-4" /> MediUX or Kometa YAML
          </div>
          <p className="mt-1 text-xs text-muted">
            On a MediUX set, copy its <em>Kometa YAML</em> and paste it here (or any Kometa metadata with <code>url_poster</code> /{" "}
            <code>url_background</code>). Posters, season posters, episode title cards and backgrounds are all imported.
          </p>
          <textarea
            className={inputClass + " mt-3 h-28 py-2 font-mono text-xs"}
            value={yamlText}
            onChange={(e) => setYamlText(e.target.value)}
            placeholder={"metadata:\n  4638:\n    url_poster: https://api.mediux.pro/assets/…"}
          />
          <Button className="mt-2" loading={fromYaml.isPending} disabled={!yamlText.trim()} onClick={() => fromYaml.mutate()}>
            Import
          </Button>
        </div>
      </div>
      <div className="mt-3 space-y-2">
        <ErrorNote error={error ?? fromFolder.error ?? fromYaml.error} />
        {latest && <SetResult job={latest} />}
      </div>
    </Card>
  );
}

function SetResult({ job }: { job: Job }) {
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
  const unmatched = (r.unmatched_files as string[] | undefined) ?? (r.unmatched_entries as string[] | undefined) ?? [];
  return (
    <div className="rounded-md border border-ok/30 bg-ok/10 px-3 py-2 text-sm">
      <div className="flex items-center gap-2 font-medium text-ok">
        <CheckCircle2 className="size-4" /> Imported {r.imported ?? 0} {job.kind === "url_artwork" ? "images" : "posters"} ·{" "}
        {formatDate(job.finished_at)}
      </div>
      <div className="mt-1 text-muted">
        Used as the base for {r.applied ?? r.imported ?? 0}
        {r.kept_choice ? `; kept your own choice for ${r.kept_choice}` : ""}.
        {r.unreadable ? ` ${r.unreadable} unreadable.` : ""}
        {r.skipped ? ` ${r.skipped} other files ignored.` : ""}
        {r.failed ? ` ${r.failed} couldn't be downloaded or read (see Activity).` : ""}
      </div>
      {unmatched.length > 0 && (
        <details className="mt-1">
          <summary className="cursor-pointer text-warn">{unmatched.length} {job.kind === "url_artwork" ? (unmatched.length === 1 ? "entry" : "entries") : unmatched.length === 1 ? "file" : "files"} didn't match anything in your library</summary>
          <ul className="mt-1 max-h-48 overflow-y-auto text-xs text-muted">
            {unmatched.map((name) => (
              <li key={name}>{name}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
