import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowDown, ArrowUp, FileUp, Layers, Plus, RotateCcw, Save, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import OverlayImages, { SortResults, type SortReport } from "../components/OverlayImages";
import { Button, Card, ErrorNote, PageHeader, Spinner, Tabs, cx, fieldBase, inputClass, selectClass, useDebounced } from "../components/ui";
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
  text: "Text",
  streaming: "Streaming logo",
  ribbon: "Ribbon",
  status: "Show status",
  audio: "Audio codec",
};
/** Kometa's ribbons: which chart or award a title is on. */
export const RIBBONS: [string, string][] = [
  ["imdb_top_250", "IMDb Top 250"],
  ["oscars_best_picture", "Oscar Best Picture"],
  ["oscars", "Any Oscar"],
  ["bafta_best_film", "BAFTA Best Film"],
  ["golden_globe_best_drama", "Golden Globe Best Drama"],
  ["cannes_palme_dor", "Palme d'Or"],
  ["berlinale_golden_bear", "Golden Bear"],
  ["venice_golden_lion", "Golden Lion"],
  ["emmys_best_drama", "Emmy Best Drama"],
  ["razzie_worst_picture", "Razzie Worst Picture"],
  ["imdb_lowest", "IMDb Lowest Rated"],
];
const badge = (type: LayerType, folder: string, h: "left" | "center" | "right", v: "top" | "center" | "bottom", extra: Partial<OverlayLayer> = {}): OverlayLayer => ({
  type,
  enabled: true,
  folder,
  h_align: h,
  v_align: v,
  h_offset: 50,
  v_offset: 50,
  scale: 1,
  ...extra,
});
/** A text layer showing a rating; "Rating" in the add-layer buttons. */
const RATING_LAYER: OverlayLayer = {
  type: "text",
  enabled: true,
  template: "IMDb {imdb_rating}",
  font: null,
  h_align: "right",
  v_align: "top",
  h_offset: 50,
  v_offset: 50,
  size: 60,
  color: "#F5C518",
  stroke_width: 2,
  stroke_color: "#000000",
};
const NEW_LAYER: Record<LayerType, OverlayLayer> = {
  text: {
    type: "text",
    enabled: true,
    template: "S{season:02}E{episode:02}  {title}",
    font: null,
    h_align: "left",
    v_align: "bottom",
    h_offset: 60,
    v_offset: 60,
    size: 70,
    color: "#FFFFFF",
    stroke_width: 2,
    stroke_color: "#000000",
  },
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
  streaming: badge("streaming", "streaming", "right", "bottom"),
  ribbon: badge("ribbon", "ribbon", "right", "bottom", { h_offset: 0, v_offset: 0, ribbons: ["imdb_top_250", "oscars_best_picture"] }),
  status: badge("status", "status", "center", "top", { h_offset: 0 }),
  audio: badge("audio", "audio_codec", "right", "top"),
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
          <OverlayImages />
          {data.pack && data.libraries.map((lib) => <LibraryCard key={lib.id} library={lib} fonts={data.pack?.fonts ?? []} />)}
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
  const [report, setReport] = useState<SortReport | null>(null);
  const replacing = useRef(false);
  const pick = (replace: boolean) => {
    if (replace && !confirm("Replace every overlay image with the ones in the zip?")) return;
    replacing.current = replace;
    fileRef.current?.click();
  };
  const upload = async (file: File) => {
    setError(null);
    const form = new FormData();
    form.append("file", file);
    if (replacing.current) form.append("replace", "true");
    setProgress(0);
    try {
      const result = await uploadWithProgress<{ sorted: SortReport }>("/api/overlays/pack", form, setProgress);
      setReport(result.sorted);
      for (const key of ["overlays", "overlay-slots", "changes"]) qc.invalidateQueries({ queryKey: [key] });
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
              <code>/config/assets/overlays/{pack.name}</code>. Adding another zip sorts its images into the slots, replacing
              those it has a newer image for.
            </p>
          ) : (
            <p className="mt-1 text-sm text-muted">
              Upload the overlay images as a zip, e.g. the jmxd pack or any zip of badges and logos. Each image is sorted into
              its slot under Overlay images below, renamed and converted as the overlays expect; anything Coverarr can't
              place is listed for you to pick.
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
        <div className="flex flex-wrap gap-2">
          <input ref={fileRef} type="file" accept=".zip" className="hidden" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
          <Button icon={<FileUp className="size-4" />} loading={progress !== null} onClick={() => pick(false)}>
            {progress !== null ? `Uploading ${Math.round(progress * 100)}%` : pack ? "Add images (.zip)" : "Upload pack (.zip)"}
          </Button>
          {pack && progress === null && (
            <Button variant="ghost" onClick={() => pick(true)}>
              Replace pack
            </Button>
          )}
        </div>
      </div>
      <ErrorNote error={error} />
      {report && <SortResults report={report} onClose={() => setReport(null)} />}
    </Card>
  );
}

function LibraryCard({ library, fonts }: { library: OverlayLibrary; fonts: string[] }) {
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
                {layer.type === "text" ? (
                  <input
                    className={inputClass + " h-8 max-w-md flex-1 text-xs"}
                    value={layer.template ?? ""}
                    onChange={(e) => update(i, { template: e.target.value })}
                    title="The text: {title}, {show}, {season}, {episode} and {year} are filled in; {season:02} pads to two digits. Also {tmdb_rating}, {imdb_rating}, {rt_rating}, {metacritic}, {runtime} and {status}; a line whose rating the title doesn't have is left out"
                  />
                ) : (
                  <input
                    className={inputClass + " h-8 max-w-xs flex-1 text-xs"}
                    value={layer.type === "image" ? layer.file ?? "" : layer.folder ?? ""}
                    onChange={(e) => update(i, layer.type === "image" ? { file: e.target.value } : { folder: e.target.value })}
                    title={layer.type === "image" ? "Image file in the pack" : "Folder in the pack"}
                  />
                )}
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
              {layer.type === "ribbon" && (
                <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-muted">
                  {RIBBONS.map(([key, label]) => (
                    <label key={key} className="flex items-center gap-1">
                      <input
                        type="checkbox"
                        checked={(layer.ribbons ?? []).includes(key)}
                        onChange={(e) =>
                          update(i, { ribbons: e.target.checked ? [...(layer.ribbons ?? []), key] : (layer.ribbons ?? []).filter((r) => r !== key) })
                        }
                      />
                      {label}
                    </label>
                  ))}
                </div>
              )}
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
                  {layer.type === "text" ? (
                    <TextStyle layer={layer} fonts={fonts} onChange={(patch) => update(i, patch)} />
                  ) : (
                    <NumberField label="scale" value={layer.scale ?? 1} step={0.05} onChange={(v) => update(i, { scale: v })} />
                  )}
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
            <Button size="sm" variant="ghost" icon={<Plus className="size-3.5" />} onClick={() => setLayers([...layers, { ...RATING_LAYER }])}>
              Rating
            </Button>
          </div>
          <p className="pt-2 text-xs text-muted">
            Layers are drawn top to bottom of this list. Positions are in pixels on a {level === "episode" ? "1920×1080" : "1000×1500"}{" "}
            image, measured in from the edge you align to. Badges pick their image by name:{" "}
            <code>resolution/4kdvhdr.png</code>, <code>edition/directors.png</code>, <code>…/Channel 4.png</code>. Text fills in{" "}
            <code>{"{title}"}</code>, <code>{"{show}"}</code>, <code>{"{season}"}</code>, <code>{"{episode}"}</code> and{" "}
            <code>{"{year}"}</code> (<code>{"{episode:02}"}</code> gives 03), and shrinks to fit the width; on episodes it makes title
            cards. Ratings (<code>{"{imdb_rating}"}</code>, <code>{"{tmdb_rating}"}</code>, <code>{"{rt_rating}"}</code>,{" "}
            <code>{"{metacritic}"}</code>), <code>{"{runtime}"}</code> and <code>{"{status}"}</code> come from TMDb (IMDb, Rotten
            Tomatoes and Metacritic need an MDBList key), as do streaming logos, show status and ribbons: fetch them with{" "}
            <em>Fetch details</em> on the dashboard, and they're kept fresh by the daily collection sync. A ribbon shows the first
            ticked chart or award the title is on.
          </p>
        </div>
        <Preview libraryId={library.id} level={level} layers={layers} />
      </div>
    </Card>
  );
}

/** Font, size, colour and outline of a text layer. */
function TextStyle({ layer, fonts, onChange }: { layer: OverlayLayer; fonts: string[]; onChange: (patch: Partial<OverlayLayer>) => void }) {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<unknown>(null);
  const addFont = async (file: File) => {
    setError(null);
    const form = new FormData();
    form.append("file", file);
    try {
      const { path } = await uploadWithProgress<{ path: string }>("/api/overlays/fonts", form, () => {});
      await qc.invalidateQueries({ queryKey: ["overlays"] });
      onChange({ font: path });
    } catch (err) {
      setError(err);
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  };
  return (
    <>
      <NumberField label="size" value={layer.size ?? 70} onChange={(v) => onChange({ size: v })} />
      <label className="flex items-center gap-1" title="Text colour">
        colour
        <input type="color" className="h-8 w-9 rounded border border-line bg-transparent" value={(layer.color ?? "#FFFFFF").slice(0, 7)} onChange={(e) => onChange({ color: e.target.value.toUpperCase() })} />
      </label>
      <NumberField label="outline" value={layer.stroke_width ?? 0} onChange={(v) => onChange({ stroke_width: v })} />
      <input
        type="color"
        title="Outline colour"
        className="h-8 w-9 rounded border border-line bg-transparent"
        value={(layer.stroke_color ?? "#000000").slice(0, 7)}
        onChange={(e) => onChange({ stroke_color: e.target.value.toUpperCase() })}
      />
      <select
        className={selectClass + " h-8 max-w-48 text-xs"}
        value={layer.font ?? ""}
        onChange={(e) => (e.target.value === "+" ? fileRef.current?.click() : onChange({ font: e.target.value || null }))}
        title="Font"
      >
        <option value="">Built-in font</option>
        {fonts.map((f) => (
          <option key={f} value={f}>
            {f.split("/").pop()}
          </option>
        ))}
        {layer.font && !fonts.includes(layer.font) && <option value={layer.font}>{layer.font} (missing)</option>}
        <option value="+">Upload a font…</option>
      </select>
      <input ref={fileRef} type="file" accept=".ttf,.otf" className="hidden" onChange={(e) => e.target.files?.[0] && addFont(e.target.files[0])} />
      {Boolean(error) && <span className="w-full text-danger">{error instanceof Error ? error.message : String(error)}</span>}
    </>
  );
}

function NumberField({ label, value, step = 1, onChange }: { label: string; value: number; step?: number; onChange: (v: number) => void }) {
  return (
    <label className="flex items-center gap-1">
      {label}
      <input
        type="number"
        step={step}
        className={fieldBase + " h-8 w-20 text-xs"}
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
      const missing = decodeURIComponent(resp.headers.get("X-Overlay-Missing") ?? "");
      return { blob: await resp.blob(), missing };
    },
    staleTime: Infinity,
    gcTime: 60_000,
  });
  // The image URL lives only while it's shown, so old previews don't pile up in memory.
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!preview.data) return;
    const next = URL.createObjectURL(preview.data.blob);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [preview.data]);
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
        {url && <img src={url} alt={sample?.title ?? "Preview"} className="h-full w-full object-cover" />}
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
