import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Clock, DownloadCloud, Fingerprint, Info, Loader2, ServerCog } from "lucide-react";
import { Link } from "react-router-dom";
import { Button, Card, EmptyState, KIND_LABEL, PageHeader, ServerChip, Spinner, formatDate } from "../components/ui";
import { api, type IssueType } from "../lib/api";
import { jobLabel, useActiveJobs } from "../lib/hooks";

const ISSUE_LABELS: Record<IssueType, string> = {
  mismatch: "Files matched to different titles",
  similar_collections: "Collections with confusingly similar names",
  collection_missing: "Collections missing on a server",
  collection_members_differ: "Collections with different members",
  poster_differs: "Posters that differ between servers",
  item_missing: "Titles missing on a server",
};

export function useStartJob() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (kind: "import" | "poster_scan" | "rematch" | "fetch_details") => api.post("/api/jobs", { kind }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["dashboard"] }),
  });
}

export default function DashboardPage() {
  const { dashboard, active, isLoading } = useActiveJobs();
  const start = useStartJob();
  if (isLoading || !dashboard) return <Spinner label="Loading" />;

  const busy = (kind: string) => active.some((j) => j.kind === kind);
  const importJob = active.find((j) => j.kind === "import");
  // While an import runs, show each server's progress instead of its last connection check.
  const importState = (serverId: number) => {
    if (!importJob) return null;
    const targets = importJob.params.server_ids as number[] | undefined;
    if (targets && !targets.includes(serverId)) return null;
    if (importJob.status === "queued") return "waiting";
    return importJob.state?.servers?.[String(serverId)] ?? "waiting";
  };
  const counts = dashboard.counts;
  const totalIssues = Object.values(dashboard.issues).reduce((a, b) => a + (b ?? 0), 0);

  if (dashboard.servers.length === 0) {
    return (
      <>
        <PageHeader title="Welcome to Coverarr" />
        <EmptyState icon={<ServerCog className="size-8" />} title="Connect your media servers">
          Add Plex, Jellyfin and Emby in Settings. Then run an import to pull every library into one place and see where
          they differ.
          <div className="mt-4">
            <Link to="/settings">
              <Button variant="primary">Open settings</Button>
            </Link>
          </div>
        </EmptyState>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle={
          importJob
            ? `Import running${importJob.message ? `: ${importJob.message}` : ""}`
            : `Last import: ${formatDate(dashboard.last_import?.finished_at)}`
        }
        actions={
          <>
            <Button
              icon={<Fingerprint className="size-4" />}
              loading={busy("poster_scan")}
              onClick={() => start.mutate("poster_scan")}
              title="Fingerprint posters that changed since the last scan"
            >
              Scan posters
            </Button>
            <Button
              icon={<Info className="size-4" />}
              loading={busy("fetch_details")}
              onClick={() => start.mutate("fetch_details")}
              title="Fetch streaming services, ratings, show status, people and ribbons from TMDb for titles that have none or week-old ones (the daily collection sync does this too)"
            >
              Fetch details
            </Button>
            <Button
              variant="primary"
              icon={<DownloadCloud className="size-4" />}
              loading={busy("import")}
              onClick={() => start.mutate("import")}
            >
              Import from servers
            </Button>
          </>
        }
      />

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        {(
          [
            ["Movies", counts.movie],
            ["Shows", counts.show],
            ["Seasons", counts.season],
            ["Episodes", counts.episode],
            ["Collections", counts.collection],
          ] as const
        ).map(([label, n]) => (
          <Card key={label} className="p-4">
            <div className="text-sm text-muted">{label}</div>
            <div className="mt-1 text-2xl font-semibold tabular-nums">{n.toLocaleString()}</div>
          </Card>
        ))}
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <Card>
          <div className="border-b border-line px-4 py-3 font-medium">Servers</div>
          <ul className="divide-y divide-line">
            {dashboard.servers.map((s) => (
              <li key={s.id} className="flex items-center gap-3 px-4 py-3">
                <ServerChip server={s} dim={!s.enabled} />
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium">{s.name}</div>
                  <div className="truncate text-xs text-muted">
                    {KIND_LABEL[s.kind]} {s.version ?? ""} · {s.items.toLocaleString()} movies &amp; shows
                  </div>
                </div>
                {importState(s.id) === "importing" ? (
                  <span className="flex items-center gap-1 text-xs text-warn">
                    <Loader2 className="size-4 animate-spin" /> Importing
                  </span>
                ) : importState(s.id) === "waiting" ? (
                  <span className="flex items-center gap-1 text-xs text-warn">
                    <Clock className="size-4" /> Waiting to import
                  </span>
                ) : s.last_error ? (
                  <span className="flex items-center gap-1 text-xs text-danger" title={s.last_error}>
                    <AlertTriangle className="size-4" /> Error
                  </span>
                ) : s.last_ok_at ? (
                  <span className="flex items-center gap-1 text-xs text-ok">
                    <CheckCircle2 className="size-4" /> OK
                  </span>
                ) : (
                  <span className="text-xs text-muted">Not imported yet</span>
                )}
              </li>
            ))}
          </ul>
        </Card>

        <Card>
          <div className="flex items-center justify-between border-b border-line px-4 py-3">
            <span className="font-medium">Differences to review</span>
            <Link to="/review" className="text-sm text-accent hover:underline">
              Open review
            </Link>
          </div>
          {totalIssues === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-muted">
              {dashboard.last_import ? "Everything matches across your servers." : "Run an import to compare your servers."}
            </div>
          ) : (
            <ul className="divide-y divide-line">
              {(Object.keys(ISSUE_LABELS) as IssueType[]).map((t) => (
                <li key={t}>
                  <Link to={`/review?type=${t}`} className="flex items-center justify-between px-4 py-3 hover:bg-panel-2">
                    <span className="text-sm">{ISSUE_LABELS[t]}</span>
                    <span className="font-semibold tabular-nums text-accent">{(dashboard.issues[t] ?? 0).toLocaleString()}</span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {dashboard.last_import?.status === "failed" && (
        <Card className="mt-6 border-danger/40 p-4 text-sm">
          <span className="font-medium text-danger">{jobLabel(dashboard.last_import)} failed:</span>{" "}
          {dashboard.last_import.error}
        </Card>
      )}
    </>
  );
}
