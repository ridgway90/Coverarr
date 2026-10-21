import { RotateCcw } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import CancelJob from "./CancelJob";
import { api } from "../lib/api";
import { useJobAction, useLibraries } from "../lib/hooks";
import { Button, Card, ErrorNote, selectClass } from "./ui";

/** Replace base posters with TMDb's main poster for everything (or one library). */
export default function TmdbReset() {
  const { data: libs } = useLibraries();
  const [libraryId, setLibraryId] = useState("");
  const [titles, setTitles] = useState(true);
  const [seasons, setSeasons] = useState(true);
  const [collections, setCollections] = useState(true);
  const [keepOwn, setKeepOwn] = useState(true);
  const [onlyMissing, setOnlyMissing] = useState(false);
  const reset = useJobAction(
    () =>
      api.post<{ job_id: number }>("/api/posters/reset-from-tmdb", {
        library_id: libraryId ? Number(libraryId) : undefined,
        kinds: [...(titles ? ["movie", "show"] : []), ...(seasons ? ["season"] : [])],
        collections,
        keep_own: keepOwn,
        only_missing: onlyMissing,
      }),
    ["changes", "items", "item", "collections", "collection", "housekeeping", "issues", "review"],
  );
  const job = reset.job;
  const result = job?.result as { replaced?: number; unchanged?: number; not_on_tmdb?: number; failed?: number; not_found?: string[] } | undefined;
  const library = libs?.libraries.find((l) => String(l.id) === libraryId);
  const start = () => {
    const scope = library ? `in ${library.name}` : "everywhere";
    const what = onlyMissing ? "titles without a base" : keepOwn ? "every poster except your uploads and poster sets" : "every poster";
    if (confirm(`Replace ${what} ${scope} with TMDb's main poster? Nothing reaches your servers until you push from Changes.`)) {
      reset.run(undefined);
    }
  };

  return (
    <Card className="p-5">
      <h2 className="flex items-center gap-2 font-semibold">
        <RotateCcw className="size-4" /> Reset posters from TMDb
      </h2>
      <p className="mt-1 max-w-3xl text-sm text-muted">
        Fetches TMDb's main poster (the one TMDb shows by default, in English) for each film, show, season and collection, stores it under{" "}
        <code>/config/assets</code> and makes it the base poster. Your other stored posters stay available on each title's page. Nothing
        is sent to your servers until you push from{" "}
        <Link to="/changes" className="text-accent hover:underline">
          Changes
        </Link>
        ; overlays, if switched on, are drawn on the new posters. Needs a TMDb API key (General tab). Films need a TMDb id; shows are
        looked up by TVDB or IMDb id if they have no TMDb one; collections are found by name.
      </p>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <div className="space-y-2 text-sm">
          <select className={selectClass} value={libraryId} onChange={(e) => setLibraryId(e.target.value)}>
            <option value="">All libraries</option>
            {(libs?.libraries ?? []).map((l) => (
              <option key={l.id} value={l.id}>
                {l.name}
              </option>
            ))}
          </select>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={titles} onChange={(e) => setTitles(e.target.checked)} /> Films and shows
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={seasons} onChange={(e) => setSeasons(e.target.checked)} /> Seasons
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={collections} onChange={(e) => setCollections(e.target.checked)} /> Collections
          </label>
        </div>
        <div className="space-y-2 text-sm">
          <label className="flex items-start gap-2">
            <input type="checkbox" className="mt-1" checked={keepOwn} onChange={(e) => setKeepOwn(e.target.checked)} />
            <span>
              Keep posters I uploaded or imported as poster sets
              <span className="block text-xs text-muted">Kometa originals, TMDb picks and server posters are still replaced.</span>
            </span>
          </label>
          <label className="flex items-start gap-2">
            <input type="checkbox" className="mt-1" checked={onlyMissing} onChange={(e) => setOnlyMissing(e.target.checked)} />
            <span>
              Only titles without a base poster
              <span className="block text-xs text-muted">Leaves every existing choice alone.</span>
            </span>
          </label>
        </div>
      </div>

      <Button className="mt-4" variant="primary" icon={<RotateCcw className="size-4" />} loading={reset.running} disabled={!titles && !seasons && !collections} onClick={start}>
        Reset from TMDb
      </Button>
      <ErrorNote error={reset.error} />
      {job && (job.status === "queued" || job.status === "running") && (
        <div className="mt-3 rounded-md border border-line bg-panel-2 px-3 py-2 text-sm">
          <div className="flex items-center justify-between gap-4">
            {job.message ?? "Queued"}
            <CancelJob job={job} />
          </div>
          <div className="mt-2 h-1 overflow-hidden rounded bg-line">
            <div className="h-full bg-accent transition-all" style={{ width: `${Math.round(job.progress * 100)}%` }} />
          </div>
        </div>
      )}
      {job?.status === "failed" && <div className="mt-3 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">{job.error}</div>}
      {job?.status === "cancelled" && (
        <div className="mt-3 rounded-md border border-line bg-panel-2 px-3 py-2 text-sm">
          Cancelled. Posters replaced before that are kept;{" "}
          <Link to="/changes" className="text-accent hover:underline">
            push them from Changes
          </Link>{" "}
          or run the reset again to carry on (titles already done are skipped quickly).
        </div>
      )}
      {job?.status === "succeeded" && result && (
        <div className="mt-3 rounded-md border border-ok/30 bg-ok/10 px-3 py-2 text-sm">
          Replaced {Number(result.replaced ?? 0).toLocaleString()} posters
          {result.unchanged ? `; ${result.unchanged.toLocaleString()} already had TMDb's poster` : ""}
          {result.failed ? `; ${result.failed} failed (see Activity)` : ""}.{" "}
          <Link to="/changes" className="text-accent hover:underline">
            Push them from Changes
          </Link>
          .
          {(result.not_found?.length ?? 0) > 0 && (
            <details className="mt-1">
              <summary className="cursor-pointer text-warn">{Number(result.not_on_tmdb ?? 0).toLocaleString()} not found on TMDb (kept as they were)</summary>
              <ul className="mt-1 max-h-48 overflow-y-auto text-xs text-muted">
                {result.not_found!.map((t) => (
                  <li key={t}>{t}</li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
    </Card>
  );
}
