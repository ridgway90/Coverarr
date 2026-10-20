import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, CheckCircle2, Search, Trash2, Upload } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  Button,
  Card,
  EmptyState,
  ErrorNote,
  PageHeader,
  Pagination,
  Poster,
  ServerChip,
  Spinner,
  inputClass,
  selectClass,
  useDebounced,
} from "../components/ui";
import { api, qs, type ChangesPage as ChangesData, type PendingChange } from "../lib/api";
import { useChangesSummary, useJobAction, useLibraries, useServerMap } from "../lib/hooks";

const KIND: Record<PendingChange["kind"], string> = {
  movie: "Film",
  show: "Show",
  season: "Season",
  episode: "Episode",
  collection: "Collection",
};

type Targets = { media_item_ids?: number[]; collection_ids?: number[] };

function targetsOf(change: PendingChange): Targets {
  return change.target === "item" ? { media_item_ids: [change.id] } : { collection_ids: [change.id] };
}

function linkTo(change: PendingChange): string {
  return change.target === "collection" ? `/collections/${change.id}` : `/items/${change.top_id ?? change.id}`;
}

export default function ChangesPage() {
  const qc = useQueryClient();
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState(params.get("q") ?? "");
  const q = useDebounced(search);
  const libraryId = params.get("library") ?? "";
  const target = params.get("target") ?? "";
  const page = Number(params.get("page") ?? 1);
  const servers = useServerMap();
  const { data: libs } = useLibraries();

  const setParam = (key: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "page") next.delete("page");
    setParams(next, { replace: true });
  };

  const { data, isLoading, isFetching, error } = useQuery({
    queryKey: ["changes", "list", q, libraryId, target, page],
    queryFn: () =>
      api.get<ChangesData>(
        `/api/changes${qs({ q, library_id: libraryId, target, page, page_size: 50 })}`,
      ),
    placeholderData: keepPreviousData,
  });
  const summary = useChangesSummary();

  const push = useJobAction((body: Targets) => api.post<{ job_id: number }>("/api/changes/push", body), [
    "changes",
    "items",
    "item",
    "collections",
    "collection",
  ]);
  const discard = useMutation({
    mutationFn: (body: Targets) => api.post("/api/changes/discard", body),
    onSuccess: () => {
      for (const key of ["changes", "items", "item", "collections", "collection", "issues", "review", "dashboard"]) {
        qc.invalidateQueries({ queryKey: [key] });
      }
    },
  });

  const total = (summary.data?.titles ?? 0) + (summary.data?.collections ?? 0);
  const uploads = summary.data?.uploads ?? 0;
  const pushAll = () => {
    if (confirm(`Upload ${uploads.toLocaleString()} poster${uploads === 1 ? "" : "s"} to your servers?`)) push.run({});
  };
  const job = push.job;
  const overlays = data?.items.some((c) => c.servers.some((s) => s.reason === "has Kometa overlays"));

  return (
    <>
      <PageHeader
        title="Changes"
        subtitle="Posters you've chosen that aren't on every server yet. Nothing is uploaded until you push."
        actions={
          <Button variant="primary" icon={<Upload className="size-4" />} loading={push.running} disabled={!uploads} onClick={pushAll}>
            Push all{uploads ? ` (${uploads.toLocaleString()})` : ""}
          </Button>
        }
      />
      <ErrorNote error={error ?? push.error ?? discard.error} />
      {push.running && job && (
        <Card className="mb-4 p-4 text-sm">
          <div className="flex items-center justify-between gap-4">
            <span>{job.message ?? "Queued"}</span>
            <span className="text-muted">{Math.round(job.progress * 100)}%</span>
          </div>
          <div className="mt-2 h-1 overflow-hidden rounded bg-line">
            <div className="h-full bg-accent transition-all" style={{ width: `${Math.round(job.progress * 100)}%` }} />
          </div>
        </Card>
      )}
      {!push.running && job && (
        <div
          className={
            job.status === "failed"
              ? "mb-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger"
              : "mb-4 rounded-md border border-line bg-panel-2 px-3 py-2 text-sm"
          }
        >
          {job.status === "failed"
            ? job.error
            : `Uploaded ${Number(job.result?.uploaded ?? 0).toLocaleString()} posters` +
              (job.result?.failed ? `; ${job.result.failed} failed (see Activity)` : ".")}
        </div>
      )}
      {overlays && (
        <p className="mb-4 rounded-md border border-warn/30 bg-warn/10 px-3 py-2 text-sm">
          Posters marked <em>has Kometa overlays</em> show the same artwork with Kometa's overlays on top. Pushing replaces
          them with your clean base and drops Kometa's <code>Overlay</code> label. Coverarr's own overlays come in the
          next update.
        </p>
      )}

      <div className="mb-5 flex flex-wrap gap-3">
        <div className="relative min-w-60 flex-1 sm:max-w-sm">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted" />
          <input
            className={inputClass + " pl-9"}
            placeholder="Search"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setParam("page", "");
            }}
          />
        </div>
        <select className={selectClass} value={libraryId} onChange={(e) => setParam("library", e.target.value)}>
          <option value="">All libraries</option>
          {(libs?.libraries ?? []).map((l) => (
            <option key={l.id} value={l.id}>
              {l.name}
            </option>
          ))}
        </select>
        <select className={selectClass} value={target} onChange={(e) => setParam("target", e.target.value)}>
          <option value="">Titles and collections</option>
          <option value="item">Titles only</option>
          <option value="collection">Collections only</option>
        </select>
      </div>

      {isLoading ? (
        <Spinner />
      ) : !data?.items.length ? (
        <EmptyState icon={<CheckCircle2 className="size-8" />} title={total ? "Nothing matches these filters" : "Everything is up to date"}>
          {total
            ? "Clear the filters to see every pending change."
            : "Choose a base poster on a title or collection (or import Kometa originals) and it will show up here."}
        </EmptyState>
      ) : (
        <Card className={isFetching ? "opacity-70 transition-opacity" : ""}>
          <ul className="divide-y divide-line">
            {data.items.map((change) => (
              <li key={`${change.target}-${change.id}`} className="flex flex-wrap items-center gap-4 p-4">
                <div className="flex items-center gap-3">
                  <Poster presence={change.base} alt={change.title} width={120} className="w-14 shrink-0" />
                  <ArrowRight className="size-4 shrink-0 text-muted" />
                  <div className="flex gap-2">
                    {change.servers.map((s) => {
                      const server = servers.get(s.server_id);
                      return (
                        <div key={s.server_id} className="w-12" title={`${server?.name ?? "Server"}: ${s.reason}`}>
                          <Poster presence={s.current} alt={s.reason} width={100} className="opacity-70" />
                          <div className="mt-1 flex justify-center">{server && <ServerChip server={server} />}</div>
                        </div>
                      );
                    })}
                  </div>
                </div>
                <div className="min-w-48 flex-1">
                  <Link to={linkTo(change)} className="font-medium hover:text-accent">
                    {change.title}
                  </Link>
                  <div className="mt-0.5 text-xs text-muted">
                    {KIND[change.kind]}
                    {change.library ? ` · ${change.library}` : ""}
                    {change.source.startsWith("artwork:") ? " · from your artwork" : ""}
                    {change.source.startsWith("server:")
                      ? ` · from ${servers.get(Number(change.source.split(":")[1]))?.name ?? "a server"}`
                      : ""}
                  </div>
                  {change.problem ? (
                    <div className="mt-1 text-xs text-danger">{change.problem}</div>
                  ) : (
                    <div className="mt-1 text-xs text-muted">
                      {change.servers
                        .map((s) => `${servers.get(s.server_id)?.name ?? "Server"}: ${s.reason}`)
                        .join(" · ")}
                    </div>
                  )}
                </div>
                <div className="flex gap-2">
                  {!change.problem && (
                    <Button size="sm" icon={<Upload className="size-3.5" />} disabled={push.running} onClick={() => push.run(targetsOf(change))}>
                      Push
                    </Button>
                  )}
                  <Button
                    size="sm"
                    variant="ghost"
                    icon={<Trash2 className="size-3.5" />}
                    disabled={discard.isPending}
                    title="Forget this choice and leave the servers as they are"
                    onClick={() => discard.mutate(targetsOf(change))}
                  >
                    Discard
                  </Button>
                </div>
              </li>
            ))}
          </ul>
          <div className="px-4 pb-4">
            <Pagination page={page} pageSize={data.page_size} total={data.total} onPage={(p) => setParam("page", String(p))} />
          </div>
        </Card>
      )}
    </>
  );
}
