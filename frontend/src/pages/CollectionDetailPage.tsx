import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, Circle, FolderPlus, Minus, Pencil, RefreshCw, Trash2, UserPlus } from "lucide-react";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import PrevNext from "../components/PrevNext";
import PosterChooser from "../components/PosterChooser";
import RenameCollection from "../components/RenameCollection";
import PendingPush from "../components/PendingPush";
import { Button, Card, ErrorNote, PageHeader, PresenceChips, ServerChip, Spinner } from "../components/ui";
import { api, type CollectionDetail } from "../lib/api";
import { useJobAction, useLibraries, useServerMap } from "../lib/hooks";
import { useBack } from "../lib/navTrail";
import { describeIssue } from "./ReviewPage";

export default function CollectionDetailPage() {
  const { collectionId } = useParams();
  const [search] = useSearchParams();
  const from = search.get("from") ?? "";
  const qc = useQueryClient();
  const servers = useServerMap();
  const { data: libs } = useLibraries();
  const [renaming, setRenaming] = useState(false);
  const { data: coll, isLoading, error } = useQuery({
    queryKey: ["collection", collectionId],
    queryFn: () => api.get<CollectionDetail>(`/api/collections/${collectionId}`),
  });
  const refresh = () => {
    for (const key of ["collection", "collections", "issues", "review", "dashboard"]) qc.invalidateQueries({ queryKey: [key] });
  };
  const choose = useMutation({
    mutationFn: (source: string | null) => api.put(`/api/collections/${collectionId}/poster-source`, { poster_source: source }),
    onSuccess: refresh,
  });
  const reload = useJobAction(() => api.post<{ job_id: number }>(`/api/collections/${collectionId}/refresh`), [
    "collection",
    "collections",
  ]);
  const removeMember = useJobAction(
    (mediaItemId: number) =>
      api.post<{ job_id: number }>(`/api/collections/${collectionId}/remove-member`, { media_item_id: mediaItemId }),
    ["collection", "collections"],
  );
  const sync = useMutation({
    mutationFn: (body: { create: boolean; add_members: boolean }) => api.post(`/api/collections/${collectionId}/sync`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dashboard"] }),
  });

  const collLibrary = libs?.libraries.find((l) => l.id === coll?.library_id);
  const back = useBack({
    to: `/collections${from ? `?${from}` : collLibrary ? `?library=${collLibrary.id}` : ""}`,
    label: "Collections",
  });

  if (isLoading) return <Spinner />;
  if (error || !coll) return <ErrorNote error={error ?? "Not found"} />;
  const library = libs?.libraries.find((l) => l.id === coll.library_id);
  const expected = library?.server_libraries.filter((s) => s.enabled).map((s) => s.server_id) ?? coll.servers.map((s) => s.server_id);
  const present = coll.servers.map((s) => s.server_id);
  const missingServers = expected.filter((id) => !present.includes(id));
  const membersDiffer = coll.issues.some((i) => i.type === "collection_members_differ" && i.status === "open");

  return (
    <>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <a href={back.href} onClick={back.go} className="inline-flex items-center gap-1 text-sm text-muted hover:text-text">
          <ArrowLeft className="size-4" /> {back.label}
        </a>
        <PrevNext kind="collections" id={coll.id} from={from} />
      </div>
      <PageHeader
        title={coll.name}
        subtitle={<PresenceChips servers={servers} presentIds={present} expectedIds={expected} />}
        actions={
          <>
            <Button
              variant="ghost"
              icon={<RefreshCw className="size-4" />}
              loading={reload.running}
              onClick={() => reload.run(undefined)}
              title="Re-read this collection from every server, without a full import"
            >
              Refresh
            </Button>
            <Button variant="ghost" icon={<Pencil className="size-4" />} onClick={() => setRenaming(true)}>
              Rename
            </Button>
            {missingServers.length > 0 && (
              <Button
                variant="primary"
                icon={<FolderPlus className="size-4" />}
                loading={sync.isPending}
                onClick={() => sync.mutate({ create: true, add_members: false })}
              >
                Create on {missingServers.map((id) => servers.get(id)?.name).join(" & ")}
              </Button>
            )}
            {membersDiffer && (
              <Button icon={<UserPlus className="size-4" />} loading={sync.isPending} onClick={() => sync.mutate({ create: false, add_members: true })}>
                Add missing members
              </Button>
            )}
          </>
        }
      />
      {renaming && <RenameCollection collectionId={coll.id} currentName={coll.name} onClose={() => setRenaming(false)} />}
      {sync.isSuccess && (
        <div className="mb-4 rounded-md border border-accent/30 bg-accent/10 px-3 py-2 text-sm">
          Queued. Watch progress in the sidebar; this page refreshes when it's done.
        </div>
      )}
      <ErrorNote error={sync.error ?? reload.error ?? removeMember.error} />
      {[reload.job, removeMember.job].map(
        (job) =>
          job?.status === "failed" && (
            <div key={job.id} className="mb-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
              {job.error}
            </div>
          ),
      )}
      {reload.job?.status === "succeeded" && (
        <div className="mb-4 rounded-md border border-line bg-panel-2 px-3 py-2 text-sm">
          {((reload.job.result?.changes as string[] | undefined) ?? []).join(" · ") || "Refreshed."}
        </div>
      )}

      <PendingPush collectionId={coll.id} />

      <div className="grid gap-6 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card className="p-5">
          <h2 className="mb-4 font-semibold">Collection poster on each server</h2>
          <PosterChooser
            title={coll.name}
            presence={coll.servers}
            artwork={coll.artwork}
            servers={servers}
            posterSource={coll.poster_source}
            onChoose={(id) => choose.mutate(id)}
            busy={choose.isPending}
            owner={{ kind: "collections", id: coll.id, sets: true }}
          />
        </Card>
        <div className="space-y-6">
          <Card className="p-5">
            <h2 className="mb-3 font-semibold">On each server</h2>
            <ul className="space-y-2 text-sm">
              {coll.servers.map((s) => {
                const server = servers.get(s.server_id);
                return (
                  <li key={s.server_id} className="flex items-center gap-2">
                    {server && <ServerChip server={server} />}
                    <span className="flex-1 truncate">{s.name}</span>
                    <span className="text-muted">
                      {s.member_count} items{s.smart && " · smart"}
                    </span>
                  </li>
                );
              })}
              {missingServers.map((id) => (
                <li key={id} className="flex items-center gap-2 text-muted">
                  {servers.get(id) && <ServerChip server={servers.get(id)!} dim />}
                  <span>Not on {servers.get(id)?.name}</span>
                </li>
              ))}
            </ul>
          </Card>
          {coll.issues.length > 0 && (
            <Card className="p-5">
              <h2 className="mb-3 font-semibold">Differences</h2>
              <ul className="space-y-2 text-sm">
                {coll.issues.map((i) => (
                  <li key={i.id} className={i.status === "ignored" ? "text-muted line-through" : ""}>
                    {describeIssue(i, servers)}
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </div>
      </div>

      <Card className="mt-6 overflow-x-auto">
        <div className="border-b border-line px-5 py-3 font-semibold">Members ({coll.members.length})</div>
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-muted">
            <tr className="border-b border-line">
              <th className="px-5 py-2 font-medium">Title</th>
              {coll.servers.map((s) => (
                <th key={s.server_id} className="px-3 py-2 text-center font-medium">
                  {servers.get(s.server_id)?.name}
                </th>
              ))}
              <th className="w-12 px-3 py-2">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {coll.members.map((m) => (
              <tr key={m.id}>
                <td className="px-5 py-2">
                  <Link to={`/items/${m.id}`} className="hover:text-accent">
                    {m.title} {m.year && <span className="text-muted">({m.year})</span>}
                  </Link>
                </td>
                {coll.servers.map((s) => {
                  const inColl = m.in_collection_on.includes(s.server_id);
                  const onServer = m.servers.some((p) => p.server_id === s.server_id);
                  return (
                    <td key={s.server_id} className="px-3 py-2 text-center">
                      {inColl ? (
                        <Check className="mx-auto size-4 text-ok" aria-label="In collection" />
                      ) : onServer ? (
                        <Circle className="mx-auto size-4 text-warn" aria-label="On server, not in collection" />
                      ) : (
                        <Minus className="mx-auto size-4 text-muted" aria-label="Not on this server" />
                      )}
                    </td>
                  );
                })}
                <td className="px-3 py-1 text-right">
                  {coll.servers.some((s) => !s.smart && m.in_collection_on.includes(s.server_id)) && (
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={<Trash2 className="size-4" />}
                      aria-label={`Remove ${m.title} from ${coll.name}`}
                      title="Remove from this collection on every server"
                      disabled={removeMember.running}
                      onClick={() => {
                        const where = coll.servers
                          .filter((s) => !s.smart && m.in_collection_on.includes(s.server_id))
                          .map((s) => servers.get(s.server_id)?.name)
                          .join(", ");
                        if (confirm(`Remove "${m.title}" from "${coll.name}" on ${where}? The film itself isn't touched.`))
                          removeMember.run(m.id);
                      }}
                    />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="flex flex-wrap gap-4 border-t border-line px-5 py-2 text-xs text-muted">
          <span className="inline-flex items-center gap-1">
            <Check className="size-3.5 text-ok" /> in collection
          </span>
          <span className="inline-flex items-center gap-1">
            <Circle className="size-3.5 text-warn" /> on server, not in collection
          </span>
          <span className="inline-flex items-center gap-1">
            <Minus className="size-3.5" /> title not on that server
          </span>
        </div>
      </Card>
    </>
  );
}
