import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Images, Plus, Trash2, Upload } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { api, qs, uploadWithProgress } from "../lib/api";
import { Button, Card, ErrorNote, Modal, Spinner, Tabs, cx, inputClass, selectClass } from "./ui";

interface Slot {
  group: "gradient" | "resolution" | "edition" | "network";
  path: string;
  label: string;
  row: string | null;
  column: string | null;
  current: string | null;
  exact: boolean;
  version: number | null;
}

interface SlotsData {
  pack: string | null;
  folders: Record<string, string>;
  slots: Slot[];
}

type Group = Slot["group"];
const GROUPS: { id: Group; label: string }[] = [
  { id: "resolution", label: "Resolution & HDR" },
  { id: "gradient", label: "Gradients" },
  { id: "edition", label: "Editions" },
  { id: "network", label: "Networks" },
];
const ROWS = [
  ["4k", "4K"],
  ["1080p", "1080p"],
  ["720p", "720p"],
  ["sd", "SD"],
] as const;
const COLUMNS = [
  ["", "SDR"],
  ["hdr", "HDR"],
  ["plus", "HDR10+"],
  ["dv", "Dolby Vision"],
  ["dvhdr", "DV + HDR"],
  ["dvhdrplus", "DV + HDR10+"],
] as const;

const REFRESH = ["overlay-slots", "overlays", "changes"];

function thumbUrl(slot: Slot): string | null {
  return slot.current ? `/api/overlays/pack/file${qs({ path: slot.current, v: slot.version })}` : null;
}

/** Every overlay image the layers can use, each uploaded on its own into the right name. */
export default function OverlayImages() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["overlay-slots"], queryFn: () => api.get<SlotsData>("/api/overlays/slots") });
  const [group, setGroup] = useState<Group>("resolution");
  const [filter, setFilter] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [bulk, setBulk] = useState<{ file: File; path: string }[] | null>(null);
  const singleRef = useRef<HTMLInputElement>(null);
  const bulkRef = useRef<HTMLInputElement>(null);
  const target = useRef<string | null>(null);

  const refresh = () => {
    for (const key of REFRESH) qc.invalidateQueries({ queryKey: [key] });
  };
  const upload = async (path: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    form.append("path", path);
    await uploadWithProgress("/api/overlays/slots", form, () => {});
  };
  const uploadOne = async (file: File) => {
    const path = target.current;
    if (!path) return;
    setError(null);
    setBusy(path);
    try {
      await upload(path, file);
      refresh();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
      if (singleRef.current) singleRef.current.value = "";
    }
  };
  const remove = useMutation({
    mutationFn: (path: string) => api.del(`/api/overlays/slots${qs({ path })}`),
    onSuccess: refresh,
  });
  const pickSeveral = async (files: FileList) => {
    setError(null);
    try {
      const guesses = await api.post<{ name: string; path: string | null }[]>("/api/overlays/slots/guess", {
        names: Array.from(files).map((f) => f.name),
      });
      setBulk(Array.from(files).map((file, i) => ({ file, path: guesses[i]?.path ?? "" })));
    } catch (err) {
      setError(err);
    } finally {
      if (bulkRef.current) bulkRef.current.value = "";
    }
  };

  const slots = data?.slots ?? [];
  const bySlot = useMemo(() => new Map(slots.map((s) => [`${s.row}|${s.column}`, s])), [slots]);
  const shown = slots.filter((s) => s.group === group && (!filter || s.label.toLowerCase().includes(filter.toLowerCase())));
  const filled = (g: Group) => slots.filter((s) => s.group === g && s.exact).length;

  const tile = (slot: Slot, compact = false) => {
    const url = thumbUrl(slot);
    return (
      <div key={slot.path} className="group relative">
        <button
          type="button"
          title={`${slot.label}: ${slot.path}${slot.current && !slot.exact ? ` (uses ${slot.current} for now)` : ""}. Click to upload.`}
          onClick={() => {
            target.current = slot.path;
            singleRef.current?.click();
          }}
          className={cx(
            "checkerboard flex w-full items-center justify-center overflow-hidden rounded-md border",
            compact ? "h-14" : "h-24",
            slot.exact ? "border-line" : "border-dashed border-line/80",
          )}
        >
          {busy === slot.path ? (
            <Spinner />
          ) : url ? (
            <img src={url} alt={slot.label} className={cx("max-h-full max-w-full object-contain p-1", !slot.exact && "opacity-40")} />
          ) : (
            <Plus className="size-5 text-muted" />
          )}
        </button>
        {!compact && <div className="mt-1 truncate text-xs" title={slot.label}>{slot.label}</div>}
        {slot.current && !slot.exact && <div className="truncate text-[11px] text-muted">uses {slot.current.split("/").pop()}</div>}
        {slot.exact && (
          <button
            type="button"
            aria-label={`Remove ${slot.label}`}
            className="absolute right-1 top-1 hidden rounded bg-bg/80 p-1 text-muted hover:text-danger group-hover:block"
            onClick={() => confirm(`Remove the ${slot.label} image?`) && remove.mutate(slot.path)}
          >
            <Trash2 className="size-3.5" />
          </button>
        )}
      </div>
    );
  };

  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="max-w-2xl">
          <h2 className="flex items-center gap-2 font-semibold">
            <Images className="size-4" /> Overlay images
          </h2>
          <p className="mt-1 text-sm text-muted">
            Upload any badge, gradient or logo on its own: click a slot and pick an image. It's converted to PNG and saved under the name
            and folder the overlays look for{data?.pack ? <> in <code>{data.pack}</code></> : " (in a new pack, if you have none yet)"},
            replacing the old one, and titles that use it are re-rendered in Changes. Faded slots borrow a simpler badge until you fill
            them; the SD badges cover 576p and 480p.
          </p>
        </div>
        <div>
          <input ref={singleRef} type="file" accept="image/png,image/jpeg,image/webp" className="hidden" onChange={(e) => e.target.files?.[0] && uploadOne(e.target.files[0])} />
          <input ref={bulkRef} type="file" multiple accept="image/png,image/jpeg,image/webp" className="hidden" onChange={(e) => e.target.files?.length && pickSeveral(e.target.files)} />
          <Button icon={<Upload className="size-4" />} onClick={() => bulkRef.current?.click()}>
            Add several images…
          </Button>
        </div>
      </div>
      <ErrorNote error={error ?? remove.error} />
      {isLoading || !data ? (
        <Spinner />
      ) : (
        <>
          <div className="mt-4">
            <Tabs<Group>
              value={group}
              onChange={(g) => {
                setGroup(g);
                setFilter("");
              }}
              tabs={GROUPS.map((g) => ({ id: g.id, label: `${g.label} (${filled(g.id)}/${slots.filter((s) => s.group === g.id).length})` }))}
            />
          </div>
          {group === "resolution" ? (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[640px] border-separate border-spacing-2 text-sm">
                <thead>
                  <tr>
                    <th />
                    {COLUMNS.map(([, label]) => (
                      <th key={label} className="text-left text-xs font-medium text-muted">
                        {label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {ROWS.map(([row, label]) => (
                    <tr key={row}>
                      <th className="pr-2 text-left text-xs font-medium text-muted">{label}</th>
                      {COLUMNS.map(([column]) => {
                        const slot = bySlot.get(`${row}|${column}`);
                        return <td key={column}>{slot && tile(slot, true)}</td>;
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="mt-1 text-xs text-muted">
                Files go in <code>{data.folders.resolution}/</code>, e.g. <code>4kdvhdr.png</code>.
              </p>
            </div>
          ) : (
            <>
              {group === "network" && (
                <input className={inputClass + " mb-3 max-w-xs"} placeholder="Find a network" value={filter} onChange={(e) => setFilter(e.target.value)} />
              )}
              {shown.length === 0 ? (
                <p className="text-sm text-muted">{group === "network" ? "No shows with a network yet; run an import." : "Nothing here."}</p>
              ) : (
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">{shown.map((s) => tile(s))}</div>
              )}
            </>
          )}
        </>
      )}
      {bulk && <BulkUpload items={bulk} slots={slots} onClose={() => setBulk(null)} upload={upload} onDone={refresh} />}
    </Card>
  );
}

function BulkUpload({
  items,
  slots,
  onClose,
  upload,
  onDone,
}: {
  items: { file: File; path: string }[];
  slots: Slot[];
  onClose: () => void;
  upload: (path: string, file: File) => Promise<void>;
  onDone: () => void;
}) {
  const [rows, setRows] = useState(items);
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const chosen = rows.filter((r) => r.path);
  const duplicates = new Set(chosen.map((r) => r.path).filter((p, i, all) => all.indexOf(p) !== i));
  const save = async () => {
    setSaving(true);
    const failed: Record<string, string> = {};
    for (const row of chosen) {
      try {
        await upload(row.path, row.file);
      } catch (err) {
        failed[row.file.name] = err instanceof Error ? err.message : String(err);
      }
    }
    setSaving(false);
    setErrors(failed);
    onDone();
    if (!Object.keys(failed).length) onClose();
  };
  return (
    <Modal
      open
      wide
      onClose={onClose}
      title={`Add ${rows.length} overlay image${rows.length === 1 ? "" : "s"}`}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={saving} disabled={!chosen.length || duplicates.size > 0} onClick={save}>
            Save {chosen.length}
          </Button>
        </>
      }
    >
      <p className="mb-3 text-sm text-muted">
        Each file's slot is guessed from its name (e.g. "4K Dolby Vision.png" is 4K + Dolby Vision). Check them and change any that
        are wrong; files left on "Skip" aren't saved.
      </p>
      <ul className="space-y-2">
        {rows.map((row, i) => (
          <li key={i} className="flex flex-wrap items-center gap-3">
            <img src={URL.createObjectURL(row.file)} alt="" className="checkerboard h-10 w-16 rounded border border-line object-contain" />
            <span className="min-w-0 flex-1 truncate text-sm" title={row.file.name}>
              {row.file.name}
            </span>
            <select
              className={cx(selectClass, "h-9 w-64", duplicates.has(row.path) && "border-danger")}
              value={row.path}
              onChange={(e) => setRows(rows.map((r, j) => (j === i ? { ...r, path: e.target.value } : r)))}
            >
              <option value="">Skip</option>
              {GROUPS.map((g) => (
                <optgroup key={g.id} label={g.label}>
                  {slots
                    .filter((s) => s.group === g.id)
                    .map((s) => (
                      <option key={s.path} value={s.path}>
                        {s.label}
                        {s.exact ? " (replace)" : ""}
                      </option>
                    ))}
                </optgroup>
              ))}
            </select>
            {errors[row.file.name] && <span className="w-full text-xs text-danger">{errors[row.file.name]}</span>}
          </li>
        ))}
      </ul>
      {duplicates.size > 0 && <p className="mt-2 text-sm text-danger">Two files are set to the same slot.</p>}
    </Modal>
  );
}
