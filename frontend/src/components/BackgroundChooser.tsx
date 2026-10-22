import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, ImagePlus, Search, Trash2, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { api, uploadWithProgress } from "../lib/api";
import { useJobAction } from "../lib/hooks";
import { Button, ErrorNote, Modal, Spinner, cx } from "./ui";

interface ServerBackground {
  server_id: number;
  server_name: string;
  server_kind: string;
  url: string | null;
  pending: string | null;
}

interface StoredBackground {
  id: number;
  source: "upload" | "tmdb";
  name: string | null;
  width: number | null;
  height: number | null;
  url: string;
}

interface ItemBackgrounds {
  chosen: string | null;
  problem: string | null;
  servers: ServerBackground[];
  stored: StoredBackground[];
}

interface TmdbBackdrop {
  file_path: string;
  thumb: string;
  width: number | null;
  height: number | null;
  language: string | null;
  votes: number;
  rating: number;
}

const REFRESH = ["backgrounds", "changes", "dashboard"];

/** The background (fanart) on each server; pick one, upload one or take one from TMDb. */
export default function BackgroundChooser({ itemId }: { itemId: number }) {
  const qc = useQueryClient();
  const refresh = () => {
    for (const key of REFRESH) qc.invalidateQueries({ queryKey: [key] });
  };
  const { data, isLoading, error } = useQuery({
    queryKey: ["backgrounds", itemId],
    queryFn: () => api.get<ItemBackgrounds>(`/api/items/${itemId}/backgrounds`),
  });
  const choose = useMutation({
    mutationFn: (source: string | null) => api.put(`/api/items/${itemId}/background`, { source }),
    onSuccess: refresh,
  });
  const remove = useMutation({ mutationFn: (id: number) => api.del(`/api/backgrounds/${id}`), onSuccess: refresh });
  const push = useJobAction(() => api.post<{ job_id: number }>("/api/changes/push", { media_item_ids: [itemId] }), [...REFRESH, "item"]);
  const fileRef = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [uploadError, setUploadError] = useState<unknown>(null);
  const [picking, setPicking] = useState(false);

  const upload = async (file: File) => {
    setUploadError(null);
    const form = new FormData();
    form.append("file", file);
    setProgress(0);
    try {
      await uploadWithProgress(`/api/items/${itemId}/background/upload`, form, setProgress);
      refresh();
    } catch (err) {
      setUploadError(err);
    } finally {
      setProgress(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  if (isLoading || !data) return <Spinner />;
  const pending = data.servers.filter((s) => s.pending);
  const tile = (key: string, url: string | null, label: string, source: string | null, extra?: React.ReactNode) => {
    const chosen = source !== null && data.chosen === source;
    return (
      <div key={key} className="group relative">
        <button
          type="button"
          disabled={!url || !source || choose.isPending}
          onClick={() => source && choose.mutate(source)}
          className={cx(
            "block aspect-video w-full overflow-hidden rounded-md bg-panel-2 ring-2 transition",
            chosen ? "ring-accent" : "ring-transparent hover:ring-accent/50",
          )}
          title={url ? (chosen ? "The background every server gets" : "Use this background on every server") : undefined}
        >
          {url ? <img src={`${url}${url.includes("?") ? "&" : "?"}w=480`} alt="" loading="lazy" className="h-full w-full object-cover" /> : <span className="text-xs text-muted">No background</span>}
        </button>
        {chosen && (
          <span className="absolute left-1.5 top-1.5 flex items-center gap-1 rounded bg-accent px-1.5 py-0.5 text-[10px] font-bold text-black">
            <Check className="size-3" /> Chosen
          </span>
        )}
        <div className="mt-1 flex items-center justify-between gap-2 text-xs text-muted">
          <span className="truncate">{label}</span>
          {extra}
        </div>
      </div>
    );
  };

  return (
    <>
      <ErrorNote error={error ?? choose.error ?? remove.error ?? uploadError ?? push.error} />
      {data.chosen && (pending.length > 0 || data.problem) && (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-accent/30 bg-accent/10 px-3 py-2 text-sm">
          {data.problem ? (
            <span className="text-danger">{data.problem}</span>
          ) : (
            <span>
              Not on <strong>{pending.map((s) => s.server_name).join(", ")}</strong> yet.
            </span>
          )}
          {!data.problem && (
            <Button variant="primary" size="sm" icon={<Upload className="size-3.5" />} loading={push.running} onClick={() => push.run(undefined)}>
              Push now
            </Button>
          )}
        </div>
      )}
      <div className="grid grid-cols-2 gap-4 lg:grid-cols-3 2xl:grid-cols-4">
        {data.servers.map((s) => tile(`s${s.server_id}`, s.url, s.server_name, s.url ? `server:${s.server_id}` : null))}
        {data.stored.map((b) =>
          tile(
            `b${b.id}`,
            b.url,
            `${b.source === "tmdb" ? "From TMDb" : "Uploaded"}${b.width ? ` · ${b.width}×${b.height}` : ""}`,
            `background:${b.id}`,
            <button
              type="button"
              className="text-muted hover:text-danger"
              aria-label="Delete this background"
              onClick={() => confirm("Delete this background? If it's the chosen one, the title's background is left as it is on each server.") && remove.mutate(b.id)}
            >
              <Trash2 className="size-3.5" />
            </button>,
          ),
        )}
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        <input ref={fileRef} type="file" accept="image/jpeg,image/png,image/webp" className="hidden" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
        <Button size="sm" icon={<ImagePlus className="size-3.5" />} loading={progress !== null} onClick={() => fileRef.current?.click()}>
          {progress !== null ? `Uploading ${Math.round(progress * 100)}%` : "Upload background"}
        </Button>
        <Button size="sm" icon={<Search className="size-3.5" />} onClick={() => setPicking(true)}>
          Find on TMDb
        </Button>
        {data.chosen && (
          <Button size="sm" variant="ghost" loading={choose.isPending} onClick={() => choose.mutate(null)} title="Leave each server's background as it is">
            Stop managing
          </Button>
        )}
      </div>
      {picking && <TmdbBackdrops itemId={itemId} onClose={() => setPicking(false)} onChosen={refresh} />}
    </>
  );
}

function TmdbBackdrops({ itemId, onClose, onChosen }: { itemId: number; onClose: () => void; onChosen: () => void }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["tmdb-backgrounds", itemId],
    queryFn: () => api.get<{ source: string; backdrops: TmdbBackdrop[] }>(`/api/items/${itemId}/tmdb-backgrounds`),
    staleTime: 5 * 60_000,
  });
  const choose = useMutation({
    mutationFn: (filePath: string) => api.post(`/api/items/${itemId}/background/tmdb`, { file_path: filePath }),
    onSuccess: () => {
      onChosen();
      onClose();
    },
  });
  return (
    <Modal open onClose={onClose} title={data?.source ? `Backgrounds from ${data.source}` : "Backgrounds from TMDb"} wide>
      {isLoading ? (
        <Spinner label="Asking TMDb" />
      ) : (
        <>
          <ErrorNote error={error ?? choose.error} />
          {data && !data.backdrops.length && <p className="py-6 text-center text-sm text-muted">TMDb has no backgrounds for this.</p>}
          <div className="grid max-h-[60vh] grid-cols-2 gap-3 overflow-y-auto sm:grid-cols-3">
            {(data?.backdrops ?? []).map((b) => (
              <button key={b.file_path} className="group text-left disabled:opacity-50" disabled={choose.isPending} onClick={() => choose.mutate(b.file_path)}>
                <div className="aspect-video overflow-hidden rounded-md bg-panel-2 ring-accent/0 transition group-hover:ring-2 group-hover:ring-accent">
                  <img src={b.thumb} alt="" loading="lazy" className="h-full w-full object-cover" />
                </div>
                <div className="mt-1 text-[11px] text-muted">
                  {b.language ? b.language.toUpperCase() : "No text"} · {b.width}×{b.height}
                  {b.votes ? ` · ★${b.rating}` : ""}
                </div>
              </button>
            ))}
          </div>
          {choose.isPending && <Spinner label="Downloading the full-size background" />}
        </>
      )}
    </Modal>
  );
}
