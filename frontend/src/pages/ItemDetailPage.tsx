import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ExternalLink, RefreshCw } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import PosterChooser from "../components/PosterChooser";
import PendingPush from "../components/PendingPush";
import { Button, Card, ErrorNote, PageHeader, PresenceChips, Spinner } from "../components/ui";
import { api, type ItemDetail } from "../lib/api";
import { useJobAction, useLibraries, useServerMap } from "../lib/hooks";
import { describeIssue } from "./ReviewPage";

export default function ItemDetailPage() {
  const { itemId } = useParams();
  const qc = useQueryClient();
  const servers = useServerMap();
  const { data: libs } = useLibraries();
  const { data: item, isLoading, error } = useQuery({
    queryKey: ["item", itemId],
    queryFn: () => api.get<ItemDetail>(`/api/items/${itemId}`),
  });
  const reload = useJobAction(() => api.post<{ job_id: number }>(`/api/items/${itemId}/refresh`), ["item", "items"]);
  const choose = useMutation({
    mutationFn: (source: string | null) => api.put(`/api/items/${itemId}/poster-source`, { poster_source: source }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["item", itemId] });
      qc.invalidateQueries({ queryKey: ["items"] });
      qc.invalidateQueries({ queryKey: ["issues"] });
      qc.invalidateQueries({ queryKey: ["review"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });

  if (isLoading) return <Spinner />;
  if (error || !item) return <ErrorNote error={error ?? "Not found"} />;
  const library = libs?.libraries.find((l) => l.id === item.library_id);
  const expected = library?.server_libraries.filter((s) => s.enabled).map((s) => s.server_id) ?? [];

  const ids = [
    item.tmdb_id && { label: `TMDb ${item.tmdb_id}`, href: `https://www.themoviedb.org/${item.kind === "show" ? "tv" : "movie"}/${item.tmdb_id}` },
    item.tvdb_id && { label: `TVDB ${item.tvdb_id}`, href: `https://thetvdb.com/dereferrer/${item.kind === "show" ? "series" : "movie"}/${item.tvdb_id}` },
    item.imdb_id && { label: `IMDb ${item.imdb_id}`, href: `https://www.imdb.com/title/${item.imdb_id}` },
  ].filter(Boolean) as { label: string; href: string }[];

  return (
    <>
      {library && (
        <Link to={`/library/${library.id}`} className="mb-3 inline-flex items-center gap-1 text-sm text-muted hover:text-text">
          <ArrowLeft className="size-4" /> {library.name}
        </Link>
      )}
      <PageHeader
        title={
          <>
            {item.title} {item.year && <span className="font-normal text-muted">({item.year})</span>}
          </>
        }
        subtitle={
          <span className="flex flex-wrap items-center gap-3">
            <PresenceChips servers={servers} presentIds={item.servers.map((s) => s.server_id)} expectedIds={expected} />
            {ids.map((i) => (
              <a key={i.label} href={i.href} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:text-text">
                {i.label} <ExternalLink className="size-3" />
              </a>
            ))}
            {!ids.length && <span className="text-warn">No TMDb/TVDB/IMDb ids: matched by title and year</span>}
          </span>
        }
        actions={
          <Button
            variant="ghost"
            icon={<RefreshCw className="size-4" />}
            loading={reload.running}
            onClick={() => reload.run(undefined)}
            title={`Re-read this ${item.kind === "show" ? "show, its seasons and episodes" : "movie"} from every server, without a full import`}
          >
            Refresh
          </Button>
        }
      />
      <ErrorNote error={reload.error} />
      {reload.job && !reload.running && (
        <div
          className={
            reload.job.status === "failed"
              ? "mb-4 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger"
              : "mb-4 rounded-md border border-line bg-panel-2 px-3 py-2 text-sm"
          }
        >
          {reload.job.status === "failed"
            ? reload.job.error
            : ((reload.job.result?.changes as string[] | undefined) ?? []).join(" · ") || "Refreshed."}
        </div>
      )}

      <PendingPush mediaItemId={item.id} />

      <div className="grid gap-6 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Card className="p-5">
          <h2 className="mb-1 font-semibold">Posters on each server</h2>
          <p className="mb-4 text-sm text-muted">
            Pick the clean base artwork. It's queued under Changes until you push it to every server.
          </p>
          <PosterChooser
            title={item.title}
            presence={item.servers}
            artwork={item.artwork}
            servers={servers}
            posterSource={item.poster_source}
            onChoose={(id) => choose.mutate(id)}
            busy={choose.isPending}
            owner={{ kind: "items", id: item.id }}
          />
          <ErrorNote error={choose.error} />
        </Card>

        <div className="space-y-6">
          {item.issues.length > 0 && (
            <Card className="p-5">
              <h2 className="mb-3 font-semibold">Differences</h2>
              <ul className="space-y-2 text-sm">
                {item.issues.map((i) => (
                  <li key={i.id} className={i.status === "ignored" ? "text-muted line-through" : ""}>
                    {describeIssue(i, servers)}
                  </li>
                ))}
              </ul>
            </Card>
          )}
          <Card className="p-5">
            <h2 className="mb-3 font-semibold">Collections</h2>
            {item.collections.length ? (
              <ul className="space-y-1 text-sm">
                {item.collections.map((c) => (
                  <li key={c.id}>
                    <Link to={`/collections/${c.id}`} className="text-accent hover:underline">
                      {c.name}
                    </Link>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted">Not in any collection.</p>
            )}
          </Card>
        </div>
      </div>

      {item.kind === "show" && (
        <Card className="mt-6">
          <div className="border-b border-line px-5 py-3 font-semibold">Seasons</div>
          <table className="w-full text-sm">
            <thead className="text-left text-xs uppercase tracking-wide text-muted">
              <tr className="border-b border-line">
                <th className="px-5 py-2 font-medium">Season</th>
                <th className="px-5 py-2 font-medium">On servers</th>
                <th className="px-5 py-2 font-medium">Episodes per server</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {item.seasons.map((s) => {
                const counts = Object.entries(s.episodes);
                const max = Math.max(0, ...counts.map(([, n]) => n));
                return (
                  <tr key={s.id}>
                    <td className="px-5 py-2 font-medium">{s.title}</td>
                    <td className="px-5 py-2">
                      <PresenceChips servers={servers} presentIds={s.servers.map((p) => p.server_id)} expectedIds={expected} />
                    </td>
                    <td className="px-5 py-2">
                      <span className="flex flex-wrap gap-3">
                        {expected.map((sid) => {
                          const n = s.episodes[String(sid)] ?? 0;
                          return (
                            <span key={sid} className={n < max ? "text-warn" : "text-muted"}>
                              {servers.get(sid)?.name}: {n}
                            </span>
                          );
                        })}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Card>
      )}
    </>
  );
}
