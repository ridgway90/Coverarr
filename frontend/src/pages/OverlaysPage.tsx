import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowDown, ArrowUp, FileUp, Layers, Plus, RotateCcw, Save, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Button, Card, ErrorNote, PageHeader, Spinner, Tabs, cx, inputClass, selectClass, useDebounced } from "../components/ui";
import { api, qs, uploadWithProgress, type LayerType, type OverlayLayer, type OverlayLibrary, type OverlayPack } from "../lib/api";

interface OverlaysData {
  pack: OverlayPack | null;
  libraries: OverlayLibrary[];
  needs_import: boolean;
}

const LEVEL_LABEL: Record<string, string> = { movie: "Films", show: "Shows", season: "Seasons", episode: "Episodes" };
const TYPE_LABEL: Record<LayerType, string> = {
  image: "Image",
  resolution: "Resolution badge",
  edition: "Edition",
  network: "Network logo",
};
const NEW_LAYER: Record<LayerType, OverlayLayer> = {
  image: { type: "image", enabled: true, file: "gradient.png" },
  resolution: { type: "resolution", enabled: true, folder: "resolution", h_align: "left", v_align: "top", h_offset: 50, v_offset: 50, scale: 1 },
  edition: { type: "edition", enabled: true, folder: "edition", h_align: "center", v_align: "bottom", h_offset: 0, v_offset: 40, scale: 1 },
  network: {
    type: "network",
    enabled: true,
    folder: "network-images/images/network/color",
    h_align: "left",
    v_align: "bottom",
    h_offset: 50,
    v_offset: 50,
    scale: 1,
  },
};

export default function OverlaysPage() {
  const { data, isLoading, error } = useQuery({ queryKey: ["overlays"], queryFn: () => api.get<OverlaysData>("/api/overlays") });
  return (
    <>
      <PageHeader
        title="Overlays"
        subtitle="Badges, gradients and logos drawn on top of each base poster. Switch a library on, then push the results from Changes."
      />
      <ErrorNote error={error} />
      {isLoading || !data ? (
        <Spinner />
      ) : (
        <div className="space-y-6">
          {data.needs_import && (
            <p className="flex items-start gap-2 rounded-md border border-warn/30 bg-warn/10 px-3 py-2 text-sm">
              <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" />
              Some titles have no resolution or HDR information yet. Run an <strong>import</strong> from the dashboard so
              badges can be chosen.
            </p>
          )}
          <PackCard pack={data.pack} />
          {data.pack && data.libraries.map((lib) => <LibraryCard key={lib.id} library={lib} />)}
        </div>
      )}
    </>
  );
}

function PackCard({ pack }: { pack: OverlayPack | null }) {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<unknown>(null);
  const upload = async (file: File) => {
    setError(null);
    const form = new FormData();
    form.append("file", file);
    setProgress(0);
    try {
      await uploadWithProgress("/api/overlays/pack", form, setProgress);
      qc.invalidateQueries({ queryKey: ["overlays"] });
      qc.invalidateQueries({ queryKey: ["changes"] });
    } catch (err) {
      setError(err);
    } finally {
      setProgress(null);
      if (fileRef.current) fileRef.current.value = "";
    }
  };
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="max-w-2xl">
          <h2 className="flex items-center gap-2 font-semibold">
            <Layers className="size-4" /> Overlay pack
          </h2>
          {pack ? (
            <p className="mt-1 text-sm text-muted">
              <strong className="text-text">{pack.name}</strong>: {pack.images.toLocaleString()} images in{" "}
              <code>/config/assets/overlays/{pack.name}</code>.
            </p>
          ) : (
            <p className="mt-1 text-sm text-muted">
              Upload the overlay images as a zip, e.g. the jmxd pack (the folder with <code>gradient_top.png</code>,{" "}
              <code>resolution/</code>, <code>edition/</code> and <code>network-images/</code>). It's unpacked into{" "}
              <code>/config/assets/overlays</code>.
            </p>
          )}
          {pack && (
            <details className="mt-2 text-xs text-muted">
              <summary className="cursor-pointer">What's in it</summary>
              <div className="mt-1">{pack.top_files.join(", ")}</div>
              <ul className="mt-1">
                {Object.entries(pack.folders)
                  .filter(([folder]) => folder !== ".")
                  .map(([folder, n]) => (
                    <li key={folder}>
                      <code>{folder}/</code>: {n}
                    </li>
                  ))}
              </ul>
            </details>
          )}
        </div>
        <div>
          <input ref={fileRef} type="file" accept=".zip" className="hidden" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
          <Button icon={<FileUp className="size-4" />} loading={progress !== null} onClick={() => fileRef.current?.click()}>
            {progress !== null ? `Uploading ${Math.round(progress * 100)}%` : pack ? "Replace pack" : "Upload pack (.zip)"}
          </Button>
        </div>
      </div>
      <ErrorNote error={error} />
    </Card>
  );
}

function LibraryCard({ library }: { library: OverlayLibrary }) {
  const qc = useQueryClient();
  const levels = Object.keys(library.levels);
  const [level, setLevel] = useState(levels[0]);
  const [enabled, setEnabled] = useState(library.enabled);
  const [draft, setDraft] = useState(library.levels);
  useEffect(() => {
    setEnabled(library.enabled);
    setDraft(library.levels);
  }, [library]);
  const dirty = enabled !== library.enabled || JSON.stringify(draft) !== JSON.stringify(library.levels);

  const refresh = () => {
    for (const key of ["overlays", "changes", "items"]) qc.invalidateQueries({ queryKey: [key] });
  };
  const save = useMutation({
    mutationFn: () => api.put(`/api/overlays/libraries/${library.id}`, { enabled, levels: draft }),
    onSuccess: refresh,
  });
  const reset = useMutation({ mutationFn: () => api.del(`/api/overlays/libraries/${library.id}`), onSuccess: refresh });

  const layers = draft[level] ?? [];
  const setLayers = (next: OverlayLayer[]) => setDraft({ ...draft, [level]: next });
  const update = (i: number, patch: Partial<OverlayLayer>) => setLayers(layers.map((l, j) => (j === i ? { ...l, ...patch } : l)));
  const move = (i: number, by: number) => {
    const next = [...layers];
    const [layer] = next.splice(i, 1);
    next.splice(i + by, 0, layer);
    setLayers(next);
  };

  return (
    <Card className="p-5">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h2 className="font-semibold">{library.name}</h2>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            Overlays on
          </label>
        </div>
        <div className="flex gap-2">
          <Button
            variant="ghost"
            size="sm"
            icon={<RotateCcw className="size-3.5" />}
            disabled={reset.isPending}
            onClick={() => confirm(`Reset ${library.name}'s overlays to the defaults (and switch them off)?`) && reset.mutate()}
          >
            Defaults
          </Button>
          <Button variant="primary" size="sm" icon={<Save className="size-3.5" />} loading={save.isPending} disabled={!dirty} onClick={() => save.mutate()}>
            Save
          </Button>
        </div>
      </div>
      <ErrorNote error={save.error ?? reset.error} />
      {save.isSuccess && !dirty && library.enabled && (
        <p className="mb-3 text-sm text-muted">
          Saved. Titles whose overlays changed are listed under{" "}
          <Link to="/changes" className="text-accent hover:underline">
            Changes
          </Link>
          , ready to push.
        </p>
      )}
      {levels.length > 1 && <Tabs value={level} onChange={setLevel} tabs={levels.map((l) => ({ id: l, label: LEVEL_LABEL[l] ?? l }))} />}

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,360px)]">
        <div className="space-y-2">
          {layers.map((layer, i) => (
            <div key={i} className={cx("rounded-md border border-line p-3", !layer.enabled && "opacity-60")}>
              <div className="flex flex-wrap items-center gap-3">
                <label className="flex items-center gap-2 text-sm font-medium">
                  <input type="checkbox" checked={layer.enabled} onChange={(e) => update(i, { enabled: e.target.checked })} />
                  {TYPE_LABEL[layer.type]}
                </label>
                <input
                  className={inputClass + " h-8 max-w-xs flex-1 text-xs"}
                  value={layer.type === "image" ? layer.file ?? "" : layer.folder ?? ""}
                  onChange={(e) => update(i, layer.type === "image" ? { file: e.target.value } : { folder: e.target.value })}
                  title={layer.type === "image" ? "Image file in the pack" : "Folder in the pack"}
                />
                <div className="ml-auto flex gap-1 text-muted">
                  <button className="hover:text-text disabled:opacity-30" disabled={i === 0} onClick={() => move(i, -1)} aria-label="Move up">
                    <ArrowUp className="size-4" />
                  </button>
                  <button
                    className="hover:text-text disabled:opacity-30"
                    disabled={i === layers.length - 1}
                    onClick={() => move(i, 1)}
                    aria-label="Move down"
                  >
                    <ArrowDown className="size-4" />
                  </button>
                  <button className="hover:text-danger" onClick={() => setLayers(layers.filter((_, j) => j !== i))} aria-label="Remove layer">
                    <Trash2 className="size-4" />
                  </button>
                </div>
              </div>
              {layer.type !== "image" && (
                <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">
                  <select className={selectClass + " h-8 text-xs"} value={layer.h_align} onChange={(e) => update(i, { h_align: e.target.value as OverlayLayer["h_align"] })}>
                    <option value="left">left</option>
                    <option value="center">centre</option>
                    <option value="right">right</option>
                  </select>
                  <select className={selectClass + " h-8 text-xs"} value={layer.v_align} onChange={(e) => update(i, { v_align: e.target.value as OverlayLayer["v_align"] })}>
                    <option value="top">top</option>
                    <option value="center">middle</option>
                    <option value="bottom">bottom</option>
                  </select>
                  <NumberField label="x" value={layer.h_offset ?? 0} onChange={(v) => update(i, { h_offset: v })} />
                  <NumberField label="y" value={layer.v_offset ?? 0} onChange={(v) => update(i, { v_offset: v })} />
                  <NumberField label="scale" value={layer.scale ?? 1} step={0.05} onChange={(v) => update(i, { scale: v })} />
                </div>
              )}
            </div>
          ))}
          <div className="flex flex-wrap gap-2 pt-1">
            {(Object.keys(NEW_LAYER) as LayerType[]).map((t) => (
              <Button key={t} size="sm" variant="ghost" icon={<Plus className="size-3.5" />} onClick={() => setLayers([...layers, { ...NEW_LAYER[t] }])}>
                {TYPE_LABEL[t]}
              </Button>
            ))}
          </div>
          <p className="pt-2 text-xs text-muted">
            Layers are drawn top to bottom of this list. Positions are in pixels on a {level === "episode" ? "1920×1080" : "1000×1500"}{" "}
            image, measured in from the edge you align to. Badges pick their image by name:{" "}
            <code>resolution/4kdvhdr.png</code>, <code>edition/directors.png</code>, <code>…/Channel 4.png</code>.
          </p>
        </div>
        <Preview libraryId={library.id} level={level} layers={layers} />
      </div>
    </Card>
  );
}

function NumberField({ label, value, step = 1, onChange }: { label: string; value: number; step?: number; onChange: (v: number) => void }) {
  return (
    <label className="flex items-center gap-1">
      {label}
      <input
        type="number"
        step={step}
        className={inputClass + " h-8 w-20 text-xs"}
        value={value}
        onChange={(e) => e.target.value !== "" && onChange(Number(e.target.value))}
      />
    </label>
  );
}

interface Sample {
  id: number;
  title: string;
  facts: Record<string, unknown>;
}

function describeFacts(f: Record<string, unknown>): string {
  const parts = [f.resolution, f.dv && "DV", f.hdr10plus ? "HDR10+" : f.hdr && "HDR", f.edition, f.network].filter(Boolean);
  return parts.length ? parts.join(" · ") : "no media info";
}

function Preview({ libraryId, level, layers }: { libraryId: number; level: string; layers: OverlayLayer[] }) {
  const [search, setSearch] = useState("");
  const q = useDebounced(search);
  const samples = useQuery({
    queryKey: ["overlays", "samples", libraryId, level, q],
    queryFn: () => api.get<Sample[]>(`/api/overlays/samples${qs({ library_id: libraryId, level, q })}`),
  });
  const [chosen, setChosen] = useState<number | null>(null);
  const sampleId = chosen ?? samples.data?.[0]?.id ?? null;
  useEffect(() => setChosen(null), [level]);
  const debouncedLayers = useDebounced(layers, 400);
  const body = useMemo(() => JSON.stringify({ media_item_id: sampleId, layers: debouncedLayers, width: 600 }), [sampleId, debouncedLayers]);
  const preview = useQuery({
    queryKey: ["overlays", "preview", body],
    enabled: sampleId !== null,
    queryFn: async () => {
      const resp = await fetch("/api/overlays/preview", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body });
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({}));
        throw new Error(detail.detail ?? `Preview failed (${resp.status})`);
      }
      const missing = resp.headers.get("X-Overlay-Missing") ?? "";
      return { url: URL.createObjectURL(await resp.blob()), missing };
    },
    staleTime: Infinity,
  });
  const sample = samples.data?.find((s) => s.id === sampleId);
  return (
    <div>
      <div className="mb-2 flex gap-2">
        <input className={inputClass + " h-8 text-xs"} placeholder="Preview on…" value={search} onChange={(e) => setSearch(e.target.value)} />
      </div>
      <select className={selectClass + " mb-2 h-8 w-full text-xs"} value={sampleId ?? ""} onChange={(e) => setChosen(Number(e.target.value))}>
        {(samples.data ?? []).map((s) => (
          <option key={s.id} value={s.id}>
            {s.title} ({describeFacts(s.facts)})
          </option>
        ))}
      </select>
      <div className={cx("relative overflow-hidden rounded-md bg-panel-2", level === "episode" ? "aspect-video" : "aspect-[2/3]")}>
        {preview.data && <img src={preview.data.url} alt={sample?.title ?? "Preview"} className="h-full w-full object-cover" />}
        {preview.isFetching && (
          <div className="absolute inset-0 flex items-center justify-center bg-black/30">
            <Spinner />
          </div>
        )}
        {!sampleId && !samples.isLoading && <div className="flex h-full items-center justify-center p-4 text-center text-sm text-muted">No titles here yet.</div>}
      </div>
      <ErrorNote error={preview.error} />
      {preview.data?.missing && <p className="mt-2 text-xs text-warn">Not in the pack: {preview.data.missing}</p>}
    </div>
  );
}
