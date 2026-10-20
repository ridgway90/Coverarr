import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, ChevronRight, Clock, Loader2, XCircle } from "lucide-react";
import { useState } from "react";
import { Card, EmptyState, Modal, PageHeader, Spinner, formatDate } from "../components/ui";
import { api, type Job } from "../lib/api";
import { jobLabel } from "../lib/hooks";

function StatusIcon({ status }: { status: Job["status"] }) {
  if (status === "succeeded") return <CheckCircle2 className="size-4 text-ok" />;
  if (status === "failed") return <XCircle className="size-4 text-danger" />;
  if (status === "running") return <Loader2 className="size-4 animate-spin text-accent" />;
  return <Clock className="size-4 text-muted" />;
}

/** Time spent so far ("Running for …") or in total ("Took …"). */
function duration(job: Job): string {
  if (!job.started_at) return job.status === "queued" ? "Waiting" : "";
  const end = job.finished_at ? new Date(job.finished_at) : new Date();
  const secs = Math.max(0, Math.round((end.getTime() - new Date(job.started_at).getTime()) / 1000));
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const text = h ? `${h}h ${m}m` : m ? `${m}m ${secs % 60}s` : `${secs}s`;
  return job.finished_at ? `Took ${text}` : `Running for ${text}`;
}

export default function JobsPage() {
  const [openId, setOpenId] = useState<number | null>(null);
  const { data, isLoading } = useQuery({
    queryKey: ["jobs"],
    queryFn: () => api.get<Job[]>("/api/jobs?limit=50"),
    refetchInterval: (q) => (q.state.data?.some((j) => j.status === "running" || j.status === "queued") ? 1500 : 10000),
  });
  return (
    <>
      <PageHeader title="Activity" subtitle="Imports, scans and changes pushed to your servers." />
      {isLoading ? (
        <Spinner />
      ) : !data?.length ? (
        <EmptyState title="No activity yet" />
      ) : (
        <Card>
          <ul className="divide-y divide-line">
            {data.map((job) => (
              <li key={job.id}>
                <button className="flex w-full items-center gap-3 px-4 py-3 text-left hover:bg-panel-2" onClick={() => setOpenId(job.id)}>
                  <StatusIcon status={job.status} />
                  <div className="min-w-0 flex-1">
                    <div className="font-medium">{jobLabel(job)}</div>
                    <div className="truncate text-xs text-muted">
                      {job.status === "failed" ? job.error : job.message ?? job.status}
                    </div>
                    {job.status === "running" && (
                      <div className="mt-2 h-1 max-w-md overflow-hidden rounded bg-line">
                        <div className="h-full bg-accent transition-all" style={{ width: `${Math.round(job.progress * 100)}%` }} />
                      </div>
                    )}
                  </div>
                  <div className="text-right text-xs text-muted">
                    <div>{formatDate(job.created_at)}</div>
                    <div>{duration(job)}</div>
                  </div>
                  <ChevronRight className="size-4 text-muted" />
                </button>
              </li>
            ))}
          </ul>
        </Card>
      )}
      {openId !== null && <JobModal id={openId} onClose={() => setOpenId(null)} />}
    </>
  );
}

function JobModal({ id, onClose }: { id: number; onClose: () => void }) {
  const { data: job } = useQuery({
    queryKey: ["jobs", id],
    queryFn: () => api.get<Job>(`/api/jobs/${id}`),
    refetchInterval: (q) => (q.state.data?.status === "running" || q.state.data?.status === "queued" ? 1500 : false),
  });
  return (
    <Modal open onClose={onClose} title={job ? `${jobLabel(job)} #${job.id}` : "Job"} wide>
      {!job ? (
        <Spinner />
      ) : (
        <div className="space-y-4 text-sm">
          <div className="flex items-center gap-2">
            <StatusIcon status={job.status} /> <span className="capitalize">{job.status}</span>
            <span className="text-muted">· {duration(job)}</span>
          </div>
          {job.error && <div className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-danger">{job.error}</div>}
          <div>
            <div className="mb-1 text-xs uppercase tracking-wide text-muted">Log</div>
            <pre className="max-h-96 overflow-auto whitespace-pre-wrap rounded-md bg-bg p-3 font-mono text-xs leading-relaxed scrollbar-thin">
              {(job.log ?? []).join("\n") || "No log lines."}
            </pre>
          </div>
        </div>
      )}
    </Modal>
  );
}
