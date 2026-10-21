import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileArchive, ImagePlus, Search } from "lucide-react";
import { useRef, useState } from "react";
import { api, uploadWithProgress } from "../lib/api";
import { useJobAction } from "../lib/hooks";
import { Button, ErrorNote, Modal, Spinner } from "./ui";

/** ``sets``: offer "Upload poster set" (films, shows and collections; not seasons). */
export type ArtworkOwner = { kind: "items" | "collections"; id: number; sets?: boolean };

interface TmdbPoster {
  file_path: string;
  thumb: string;
  width: number | null;
  height: number | null;
  language: string | null;
  votes: number;
  rating: number;
}

const REFRESH = ["item", "items", "collection", "collections", "changes", "issues", "review", "dashboard"];

export function useArtworkRefresh() {
  const qc = useQueryClient();
  return () => {
    for (const key of REFRESH) qc.invalidateQueries({ queryKey: [key] });
  };
}

/** "Upload poster" and "Find on TMDb" for a title or collection. */
export default function ArtworkTools({ owner, allowTmdb = true }: { owner: ArtworkOwner; allowTmdb?: boolean }) {
  const refresh = useArtworkRefresh();
  const fileRef = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [picking, setPicking] = useState(false);
  const setRef = useRef<HTMLInputElement>(null);
  const [setProgressPct, setSetProgress] = useState<number | null>(null);
  const posterSet = useJobAction(async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    form.append(owner.kind === "items" ? "media_item_id" : "collection_id", String(owner.id));
    setSetProgress(0);
    try {
      return await uploadWithProgress<{ job_id: number }>("/api/poster-sets/upload", form, setSetProgress);
    } finally {
      setSetProgress(null);
      if (setRef.current) setRef.current.value = "";
    }
  }, REFRESH);
  const setResult = posterSet.job?.status === "succeeded" ? (posterSet.job.result as { imported?: number; unmatched_files?: string[] }) : null;

  const upload = async (file: File) => {
    setError(null);
    const form = new FormData();
    form.append("file", file);
    setProgress(0);
    try {
      await uploadWithProgress(`/api/${owner.kind}/${owner.id}/artwork`, form, setProgress);
      refresh();
    } catch (err) {
      setError(err);
    } finally {
      setProgress(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <div className="mb-4">
      <div className="flex flex-wrap gap-2">
        <input
          ref={fileRef}
          type="file"
          accept="image/jpeg,image/png,image/webp"
          className="hidden"
          onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])}
        />
        <Button
          size="sm"
          icon={<ImagePlus className="size-3.5" />}
          loading={progress !== null}
          onClick={() => fileRef.current?.click()}
          title="Use your own image as the base poster on every server"
        >
          {progress !== null ? `Uploading ${Math.round(progress * 100)}%` : "Upload poster"}
        </Button>
        {allowTmdb && (
          <Button size="sm" icon={<Search className="size-3.5" />} onClick={() => setPicking(true)}>
            Find on TMDb
          </Button>
        )}
        {owner.sets && (
          <>
            <input
              ref={setRef}
              type="file"
              accept=".zip,application/zip"
              className="hidden"
              onChange={(e) => e.target.files?.[0] && posterSet.run(e.target.files[0])}
            />
            <Button
              size="sm"
              icon={<FileArchive className="size-3.5" />}
              loading={posterSet.running}
              onClick={() => setRef.current?.click()}
              title={
                owner.kind === "items"
                  ? "A zip of posters (e.g. from ThePosterDB): the main poster and, for a show, its seasons"
                  : "A zip of posters (e.g. from ThePosterDB): the collection's poster and its films'"
              }
            >
              {setProgressPct !== null ? `Uploading ${Math.round(setProgressPct * 100)}%` : posterSet.running ? "Importing set…" : "Upload poster set"}
            </Button>
          </>
        )}
      </div>
      <ErrorNote error={error ?? posterSet.error} />
      {posterSet.job?.status === "failed" && <div className="mt-2 text-sm text-danger">{posterSet.job.error}</div>}
      {setResult && (
        <div className="mt-2 text-sm text-muted">
          Imported {setResult.imported ?? 0} poster{setResult.imported === 1 ? "" : "s"} from the set; each is now the base and waits in Changes.
          {(setResult.unmatched_files?.length ?? 0) > 0 && (
            <span className="text-warn"> Not for {owner.kind === "items" ? "this title" : "this collection"}: {setResult.unmatched_files!.join(", ")}.</span>
          )}
        </div>
      )}
      {picking && <TmdbPicker owner={owner} onClose={() => setPicking(false)} onChosen={refresh} />}
    </div>
  );
}

function TmdbPicker({ owner, onClose, onChosen }: { owner: ArtworkOwner; onClose: () => void; onChosen: () => void }) {
  const [language, setLanguage] = useState<"preferred" | "all">("preferred");
  const { data, isLoading, error } = useQuery({
    queryKey: ["tmdb-posters", owner.kind, owner.id],
    queryFn: () => api.get<{ source: string; posters: TmdbPoster[] }>(`/api/${owner.kind}/${owner.id}/tmdb-posters`),
    staleTime: 5 * 60_000,
  });
  const choose = useMutation({
    mutationFn: (filePath: string) => api.post(`/api/${owner.kind}/${owner.id}/artwork/tmdb`, { file_path: filePath }),
    onSuccess: () => {
      onChosen();
      onClose();
    },
  });
  const posters = (data?.posters ?? []).filter((p) => language === "all" || p.language === "en" || p.language === null);

  return (
    <Modal open onClose={onClose} title={data?.source ? `Posters from ${data.source}` : "Posters from TMDb"} wide>
      {isLoading ? (
        <Spinner label="Asking TMDb" />
      ) : (
        <>
          <ErrorNote error={error ?? choose.error} />
          {data && (
            <div className="mb-3 flex items-center justify-between gap-3 text-sm text-muted">
              <span>
                {posters.length} of {data.posters.length} posters. Pick one to use it as the base poster.
              </span>
              <select
                className="rounded border border-line bg-panel-2 px-2 py-1 text-xs"
                value={language}
                onChange={(e) => setLanguage(e.target.value as "preferred" | "all")}
              >
                <option value="preferred">English and textless</option>
                <option value="all">All languages</option>
              </select>
            </div>
          )}
          {data && !posters.length && <p className="py-6 text-center text-sm text-muted">TMDb has no posters for this.</p>}
          <div className="grid max-h-[60vh] grid-cols-3 gap-3 overflow-y-auto sm:grid-cols-4">
            {posters.map((p) => (
              <button
                key={p.file_path}
                className="group text-left disabled:opacity-50"
                disabled={choose.isPending}
                onClick={() => choose.mutate(p.file_path)}
              >
                <div className="aspect-[2/3] overflow-hidden rounded-md bg-panel-2 ring-accent/0 transition group-hover:ring-2 group-hover:ring-accent">
                  <img src={p.thumb} alt="" loading="lazy" className="h-full w-full object-cover" />
                </div>
                <div className="mt-1 text-[11px] text-muted">
                  {p.language ? p.language.toUpperCase() : "No text"} · {p.width}×{p.height}
                  {p.votes ? ` · ★${p.rating}` : ""}
                </div>
              </button>
            ))}
          </div>
          {choose.isPending && <Spinner label="Downloading the full-size poster" />}
        </>
      )}
    </Modal>
  );
}
