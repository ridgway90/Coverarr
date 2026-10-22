import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Save, Upload } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useJobAction } from "../lib/hooks";
import { Button, ErrorNote, Field, Spinner, inputClass } from "./ui";

interface Edits {
  title?: string;
  sort_title?: string;
  summary?: string;
  labels?: string[];
}

interface ItemMetadata {
  edits: Edits;
  servers: { server_id: number; server_name: string; title: string; sort_title: string | null; labels: string[]; pending: string | null }[];
}

const REFRESH = ["metadata", "changes", "dashboard", "item", "items"];

/** Title, sort title, summary and labels set once and pushed to every server. */
export default function MetadataEditor({ itemId }: { itemId: number }) {
  const qc = useQueryClient();
  const refresh = () => {
    for (const key of REFRESH) qc.invalidateQueries({ queryKey: [key] });
  };
  const { data, isLoading, error } = useQuery({
    queryKey: ["metadata", itemId],
    queryFn: () => api.get<ItemMetadata>(`/api/items/${itemId}/metadata`),
  });
  const [form, setForm] = useState({ title: "", sort_title: "", summary: "", labels: "" });
  useEffect(() => {
    if (data) {
      const e = data.edits;
      setForm({ title: e.title ?? "", sort_title: e.sort_title ?? "", summary: e.summary ?? "", labels: (e.labels ?? []).join(", ") });
    }
  }, [data]);
  const save = useMutation({ mutationFn: () => api.put(`/api/items/${itemId}/metadata`, form), onSuccess: refresh });
  const push = useJobAction(() => api.post<{ job_id: number }>("/api/changes/push", { media_item_ids: [itemId] }), REFRESH);

  if (isLoading || !data) return <Spinner />;
  const saved = data.edits;
  const dirty =
    form.title !== (saved.title ?? "") ||
    form.sort_title !== (saved.sort_title ?? "") ||
    form.summary !== (saved.summary ?? "") ||
    form.labels !== (saved.labels ?? []).join(", ");
  const pending = data.servers.filter((s) => s.pending);
  const set = (key: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [key]: e.target.value });
  const first = data.servers[0];

  return (
    <>
      <ErrorNote error={error ?? save.error ?? push.error} />
      {!dirty && pending.length > 0 && (
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3 rounded-md border border-accent/30 bg-accent/10 px-3 py-2 text-sm">
          <span>
            Not on <strong>{pending.map((s) => s.server_name).join(", ")}</strong> yet
            <span className="text-muted"> ({[...new Set(pending.map((s) => s.pending))].join(", ")})</span>.
          </span>
          <Button variant="primary" size="sm" icon={<Upload className="size-3.5" />} loading={push.running} onClick={() => push.run(undefined)}>
            Push now
          </Button>
        </div>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Title" hint={first ? `Now: ${[...new Set(data.servers.map((s) => s.title))].join(" / ")}` : undefined}>
          <input className={inputClass} value={form.title} onChange={set("title")} placeholder="Leave blank to keep each server's" />
        </Field>
        <Field label="Sort title" hint="e.g. a series number so films sort in order.">
          <input className={inputClass} value={form.sort_title} onChange={set("sort_title")} />
        </Field>
      </div>
      <div className="mt-4">
        <Field label="Summary">
          <textarea className={inputClass + " h-24 py-2"} value={form.summary} onChange={set("summary")} />
        </Field>
      </div>
      <div className="mt-4">
        <Field label="Labels" hint="Comma-separated; tags on Jellyfin/Emby. Labels you remove here come off the servers; ones set there by hand stay.">
          <input className={inputClass} value={form.labels} onChange={set("labels")} placeholder="Kids, Favourites" />
        </Field>
      </div>
      <div className="mt-4 flex items-center gap-3">
        <Button variant="primary" size="sm" icon={<Save className="size-3.5" />} loading={save.isPending} disabled={!dirty} onClick={() => save.mutate()}>
          Save
        </Button>
        <span className="text-xs text-muted">Blank fields aren't managed: each server keeps its own. Saved edits wait under Changes until you push.</span>
      </div>
    </>
  );
}
