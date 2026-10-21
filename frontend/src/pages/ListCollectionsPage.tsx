import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ExternalLink, ListChecks, ListPlus, Pencil, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import CancelJob from "../components/CancelJob";
import { Button, Card, EmptyState, ErrorNote, Field, Modal, PageHeader, PresenceChips, Spinner, cx, formatDate, inputClass, selectClass } from "../components/ui";
import { api } from "../lib/api";
import { MODE_LABELS, ORDER_LABELS, SOURCE_BY_TYPE, SOURCE_TYPES, builderLink, type Builder, type Definition } from "../lib/definitions";
import { useJobAction, useLibraries, useServerMap } from "../lib/hooks";

const REFRESH = ["definitions", "collections", "collection", "changes", "issues", "review"];

export default function ListCollectionsPage() {
  const qc = useQueryClient();
  const { data: libs } = useLibraries();
  const servers = useServerMap();
  const { data, isLoading } = useQuery({ queryKey: ["definitions"], queryFn: () => api.get<Definition[]>("/api/definitions") });
  const [editing, setEditing] = useState<Definition | "new" | null>(null);
  const sync = useJobAction((ids?: number[]) => api.post<{ job_id: number }>("/api/definitions/sync", { ids }), REFRESH);
  const check = useJobAction((ids?: number[]) => api.post<{ job_id: number }>("/api/definitions/check", { ids }), ["definitions"]);
  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) => api.put(`/api/definitions/${id}`, { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["definitions"] }),
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/api/definitions/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["definitions"] }),
  });
  const libraryName = (id: number | null) => libs?.libraries.find((l) => l.id === id)?.name;
  const job = sync.running ? sync.job : check.running ? check.job : undefined;
  const busy = sync.running || check.running;
  const on = (data ?? []).filter((d) => d.enabled).length;

  return (
    <>
      <PageHeader
        title="List collections"
        subtitle="Collections built from TMDb and Trakt lists and charts, kept the same on every server."
        actions={
          <>
            <Button icon={<RefreshCw className="size-4" />} loading={sync.running} disabled={!on} onClick={() => sync.run(undefined)}>
              Sync all switched on{on ? ` (${on})` : ""}
            </Button>
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditing("new")}>
              New collection
            </Button>
          </>
        }
      />
      <p className="-mt-2 mb-4 max-w-3xl text-sm text-muted">
        A sync creates the collection on every server where it's missing and adds the listed titles you have. In <em>exact</em> mode it also
        removes titles that have left the lists (never when a list fails to load). Collections that are switched on sync daily (Settings →
        Automation) and in the weekly scan. A new collection from a TMDb collection gets TMDb's poster; choose any other poster on its
        collection page.
      </p>
      {busy && (
        <Card className="mb-4 flex items-center justify-between gap-4 p-3 text-sm">
          <span>{job?.message ?? "Queued"}</span>
          <CancelJob job={job} />
        </Card>
      )}
      <ErrorNote error={sync.error ?? check.error ?? toggle.error ?? remove.error} />
      {isLoading ? (
        <Spinner />
      ) : !data?.length ? (
        <EmptyState icon={<ListPlus className="size-8" />} title="No list collections yet">
          Make one with <strong>New collection</strong>, or import your Kometa collection files in Settings → Import from Kometa.
        </EmptyState>
      ) : (
        <Card>
          <ul className="divide-y divide-line">
            {data.map((d) => (
              <DefinitionRow
                key={d.id}
                d={d}
                library={libraryName(d.library_id)}
                servers={servers}
                busy={busy}
                onToggle={(enabled) => toggle.mutate({ id: d.id, enabled })}
                onSync={() => sync.run([d.id])}
                onCheck={() => check.run([d.id])}
                onEdit={() => setEditing(d)}
                onRemove={() => confirm(`Remove "${d.name}" from Coverarr? The collection stays on your servers as it is.`) && remove.mutate(d.id)}
              />
            ))}
          </ul>
        </Card>
      )}
      {editing && <DefinitionForm definition={editing === "new" ? null : editing} onClose={() => setEditing(null)} />}
    </>
  );
}

function DefinitionRow({
  d,
  library,
  servers,
  busy,
  onToggle,
  onSync,
  onCheck,
  onEdit,
  onRemove,
}: {
  d: Definition;
  library: string | undefined;
  servers: ReturnType<typeof useServerMap>;
  busy: boolean;
  onToggle: (enabled: boolean) => void;
  onSync: () => void;
  onCheck: () => void;
  onEdit: () => void;
  onRemove: () => void;
}) {
  const sync = d.sync_result;
  const check = d.check_result;
  const newer = sync && (!check || (d.synced_at ?? "") >= (d.checked_at ?? ""));
  const errors = (newer ? sync?.errors : check?.errors) ?? [];
  const missing = (newer ? sync?.missing : check?.missing) ?? [];
  const missingCount = (newer ? sync?.missing_count : check?.missing_count) ?? 0;
  const options = [
    ORDER_LABELS[d.options.collection_order ?? "release"],
    d.options.sync_mode === "sync" ? "exact" : "add only",
    d.options.sort_title && `sort title "${d.options.sort_title}"`,
  ].filter(Boolean);

  return (
    <li className="p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="font-medium">
              {d.collection ? (
                <Link to={`/collections/${d.collection.id}`} className="hover:text-accent">
                  {d.name}
                </Link>
              ) : (
                d.name
              )}
            </span>
            <span className="text-xs text-muted">
              {library ?? "No library chosen"}
              {d.source === "kometa" && ` · from Kometa${d.source_file ? ` (${d.source_file})` : ""}`}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs">
            {d.builders.map((b) => {
              const link = builderLink(b);
              return link.href ? (
                <a key={`${b.type}:${b.value}`} href={link.href} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-accent hover:underline">
                  {link.label} <ExternalLink className="size-3" />
                </a>
              ) : (
                <span key={`${b.type}:${b.value}`}>{link.label}</span>
              );
            })}
          </div>
          <div className="mt-1 text-xs text-muted">{options.join(" · ")}</div>
          {d.unsupported.length > 0 && (
            <div className="mt-1 flex items-center gap-1 text-xs text-warn">
              <AlertTriangle className="size-3.5" /> Kometa settings Coverarr ignores: {d.unsupported.join(", ")}
            </div>
          )}
          <div className="mt-1 flex items-center gap-2 text-xs text-muted">
            {d.collection ? (
              <>
                On <PresenceChips servers={servers} presentIds={d.collection.servers} expectedIds={d.collection.servers} />
              </>
            ) : (
              "Not on your servers yet"
            )}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="mr-2 flex items-center gap-2 text-sm" title="Sync daily and in the weekly scan">
            <input type="checkbox" checked={d.enabled} disabled={!d.library_id} onChange={(e) => onToggle(e.target.checked)} />
            Keep in sync
          </label>
          <Button size="sm" icon={<RefreshCw className="size-3.5" />} disabled={busy || !d.library_id} onClick={onSync}>
            Sync now
          </Button>
          <Button size="sm" variant="ghost" icon={<ListChecks className="size-3.5" />} disabled={busy || !d.library_id} onClick={onCheck} title="Compare the lists with your library without changing anything">
            Check
          </Button>
          <Button size="sm" variant="ghost" icon={<Pencil className="size-3.5" />} aria-label={`Edit ${d.name}`} onClick={onEdit} />
          <Button size="sm" variant="ghost" icon={<Trash2 className="size-3.5" />} aria-label={`Remove ${d.name}`} onClick={onRemove} />
        </div>
      </div>

      {(sync || check) && (
        <div className={cx("mt-3 rounded-md border px-3 py-2 text-sm", errors.length ? "border-warn/40 bg-warn/5" : "border-line bg-panel-2")}>
          {newer && sync ? (
            <>
              <div>
                <span className="font-medium">{sync.in_library ?? 0}</span> of {sync.total ?? 0} listed titles are in {library ?? "the library"}
                <span className="text-muted"> · synced {formatDate(d.synced_at)}</span>
              </div>
              {Object.keys(sync.servers).length > 0 && (
                <div className="mt-1 flex flex-wrap gap-3 text-xs text-muted">
                  {Object.entries(sync.servers).map(([name, o]) => (
                    <span key={name}>
                      {name}:{" "}
                      {o.error ? (
                        <span className="text-danger">{o.error}</span>
                      ) : o.skipped ? (
                        o.skipped
                      ) : (
                        <>
                          {o.created && "created, "}
                          {o.added} added{o.removed ? `, ${o.removed} removed` : ""}
                        </>
                      )}
                    </span>
                  ))}
                </div>
              )}
              {sync.notes.map((n) => (
                <div key={n} className="mt-1 text-xs text-muted">
                  {n}
                </div>
              ))}
            </>
          ) : (
            check && (
              <div>
                <span className="font-medium">{check.in_library}</span> of {check.total} listed titles are in {library ?? "the library"}
                <span className="text-muted"> · checked {formatDate(d.checked_at)}</span>
              </div>
            )
          )}
          {missing.length > 0 && (
            <details className="mt-1 text-xs text-muted">
              <summary className="cursor-pointer">{missingCount} not in your library</summary>
              {missing.map((m) => `${m.title}${m.year ? ` (${m.year})` : ""}`).join(", ")}
              {missingCount > missing.length && ` and ${missingCount - missing.length} more`}
            </details>
          )}
          {errors.map((e) => (
            <div key={e} className="mt-1 text-xs text-warn">
              {e}
            </div>
          ))}
        </div>
      )}
    </li>
  );
}

function DefinitionForm({ definition, onClose }: { definition: Definition | null; onClose: () => void }) {
  const qc = useQueryClient();
  const { data: libs } = useLibraries();
  const libraries = libs?.libraries ?? [];
  const [name, setName] = useState(definition?.name ?? "");
  const [libraryId, setLibraryId] = useState<number | "">(definition?.library_id ?? libraries[0]?.id ?? "");
  const [builders, setBuilders] = useState<Builder[]>(definition?.builders.length ? definition.builders : [{ type: "tmdb_list", value: "" }]);
  const [mode, setMode] = useState(definition?.options.sync_mode ?? "append");
  const [order, setOrder] = useState(definition?.options.collection_order ?? "release");
  const [sortTitle, setSortTitle] = useState(definition?.options.sort_title ?? "");
  const [summary, setSummary] = useState(definition?.options.summary ?? "");
  const [enabled, setEnabled] = useState(definition?.enabled ?? true);
  const kind = libraries.find((l) => l.id === libraryId)?.kind ?? "movie";
  const types = SOURCE_TYPES.filter((s) => s.kinds.includes(kind));

  const save = useMutation({
    mutationFn: () => {
      const body = {
        name,
        library_id: libraryId === "" ? null : libraryId,
        builders: builders.filter((b) => String(b.value).trim()),
        options: { sync_mode: mode, collection_order: order, sort_title: sortTitle, summary },
        enabled,
      };
      return definition ? api.put(`/api/definitions/${definition.id}`, body) : api.post("/api/definitions", body);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["definitions"] });
      qc.invalidateQueries({ queryKey: ["jobs"] });
      onClose();
    },
  });
  const setBuilder = (i: number, patch: Partial<Builder>) => setBuilders(builders.map((b, j) => (j === i ? { ...b, ...patch } : b)));

  return (
    <Modal
      open
      wide
      onClose={onClose}
      title={definition ? `Edit ${definition.name}` : "New list collection"}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" loading={save.isPending} disabled={!name.trim() || libraryId === "" || !builders.some((b) => String(b.value).trim())} onClick={() => save.mutate()}>
            Save
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name" hint={definition?.collection ? "Renaming also renames the collection on your servers." : undefined}>
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          </Field>
          <Field label="Library">
            <select className={selectClass + " w-full"} value={libraryId} onChange={(e) => setLibraryId(e.target.value ? Number(e.target.value) : "")}>
              {libraries.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <div>
          <div className="mb-1 text-sm font-medium">Lists</div>
          <p className="mb-2 text-xs text-muted">Titles from every list are merged. Charts take how many titles to include; TMDb needs an API key and Trakt a client ID (Settings → General).</p>
          <div className="space-y-2">
            {builders.map((b, i) => {
              const source = SOURCE_BY_TYPE.get(b.type);
              return (
                <div key={i} className="flex gap-2">
                  <select
                    className={selectClass + " w-56 shrink-0"}
                    value={b.type}
                    onChange={(e) => setBuilder(i, { type: e.target.value, value: SOURCE_BY_TYPE.get(e.target.value)?.chart ? "20" : "" })}
                  >
                    {!types.some((t) => t.type === b.type) && <option value={b.type}>{source?.label ?? b.type}</option>}
                    {types.map((t) => (
                      <option key={t.type} value={t.type}>
                        {t.label}
                      </option>
                    ))}
                  </select>
                  <input
                    className={inputClass}
                    type={source?.chart ? "number" : "text"}
                    min={1}
                    max={500}
                    placeholder={source?.hint}
                    value={b.value}
                    onChange={(e) => setBuilder(i, { value: e.target.value })}
                  />
                  <Button variant="ghost" icon={<X className="size-4" />} aria-label="Remove this list" disabled={builders.length === 1} onClick={() => setBuilders(builders.filter((_, j) => j !== i))} />
                </div>
              );
            })}
          </div>
          <Button size="sm" variant="ghost" className="mt-2" icon={<Plus className="size-3.5" />} onClick={() => setBuilders([...builders, { type: types[0]?.type ?? "tmdb_list", value: "" }])}>
            Add a list
          </Button>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Sync" hint="Add only never removes anything from the collection.">
            <select className={selectClass + " w-full"} value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
              {Object.entries(MODE_LABELS).map(([k, label]) => (
                <option key={k} value={k}>
                  {label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Order" hint="List order is kept on Plex; Jellyfin and Emby sort list-ordered collections by name.">
            <select className={selectClass + " w-full"} value={order} onChange={(e) => setOrder(e.target.value as typeof order)}>
              {Object.entries(ORDER_LABELS).map(([k, label]) => (
                <option key={k} value={k}>
                  {label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Sort title" hint={'Optional, e.g. "!01 Edgar Wright" to list it first.'}>
            <input className={inputClass} value={sortTitle} onChange={(e) => setSortTitle(e.target.value)} />
          </Field>
          <label className="flex items-start gap-2 pt-7 text-sm">
            <input type="checkbox" className="mt-1" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />
            <span>
              Keep in sync
              <span className="block text-xs text-muted">Daily and in the weekly scan.</span>
            </span>
          </label>
        </div>
        <Field label="Summary">
          <textarea className={inputClass + " h-20 py-2"} value={summary} onChange={(e) => setSummary(e.target.value)} />
        </Field>
        <ErrorNote error={save.error} />
      </div>
    </Modal>
  );
}
