import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Check, Circle, FolderPlus, Minus, Pencil, UserPlus } from "lucide-react";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import PosterChooser from "../components/PosterChooser";
import RenameCollection from "../components/RenameCollection";
import { Button, Card, ErrorNote, PageHeader, PresenceChips, ServerChip, Spinner } from "../components/ui";
import { api, type CollectionDetail } from "../lib/api";
import { useLibraries, useServerMap } from "../lib/hooks";
import { describeIssue } from "./ReviewPage";

export default function CollectionDetailPage() {
  const { collectionId } = useParams();
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
    mutationFn: (serverId: number | null) =>
      api.put(`/api/collections/${collectionId}/poster-source`, { poster_source: serverId === null ? null : `server:${serverId}` }),
    onSuccess: refresh,
  });
  const sync = useMutation({
    mutationFn: (body: { create: boolean; add_members: boolean }) => api.post(`/api/collections/${collectionId}/sync`, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dashboard"] }),
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
      <Link to={`/collections${library ? `?library=${library.id}` : ""}`} className="mb-3 inline-flex items-center gap-1 text-sm text-muted hover:text-text">
        <ArrowLeft className="size-4" /> Collections
      </Link>
      <PageHeader
        title={coll.name}
        subtitle={<PresenceChips servers={servers} presentIds={present} expectedIds={expected} />}
        actions={
          <>
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
      <ErrorNote error={sync.error} />

      <div className="grid gap-6 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card className="p-5">
          <h2 className="mb-4 font-semibold">Collection poster on each server</h2>
          <PosterChooser
            title={coll.name}
            presence={coll.servers}
            servers={servers}
            posterSource={coll.poster_source}
            onChoose={(id) => choose.mutate(id)}
            busy={choose.isPending}
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
