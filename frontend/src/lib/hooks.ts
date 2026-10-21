import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, type ChangesSummary, type Dashboard, type Job, type Library, type Server } from "./api";

export function useServers() {
  return useQuery({ queryKey: ["servers"], queryFn: () => api.get<Server[]>("/api/servers") });
}

export function useServerMap() {
  const { data } = useServers();
  return new Map((data ?? []).map((s) => [s.id, s]));
}

export function useLibraries() {
  return useQuery({
    queryKey: ["libraries"],
    queryFn: () => api.get<{ libraries: Library[]; unmapped: Library["server_libraries"] }>("/api/libraries"),
  });
}

/** Polls running jobs; when one finishes, refreshes everything that might have changed. */
export function useActiveJobs() {
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api.get<Dashboard>("/api/dashboard"),
    refetchInterval: (q) => ((q.state.data?.active_jobs.length ?? 0) > 0 ? 1500 : 15000),
  });
  const active = query.data?.active_jobs ?? [];
  const previous = useRef<number[]>([]);
  useEffect(() => {
    const ids = active.map((j) => j.id);
    const finished = previous.current.filter((id) => !ids.includes(id));
    previous.current = ids;
    if (finished.length) {
      for (const key of ["items", "item", "collections", "collection", "review", "issues", "libraries", "jobs", "servers", "changes"]) {
        qc.invalidateQueries({ queryKey: [key] });
      }
    }
  }, [active, qc]);
  return { active, dashboard: query.data, isLoading: query.isLoading };
}

export const JOB_LABELS: Record<string, string> = {
  import: "Import from servers",
  poster_scan: "Poster fingerprint scan",
  rematch: "Re-match libraries",
  collection_sync: "Update collections",
  fix_match: "Fix match",
  collection_rename: "Rename collection",
  kometa_originals: "Import Kometa originals",
  refresh_item: "Refresh title",
  refresh_collection: "Refresh collection",
  collection_remove: "Remove from collection",
  check_definitions: "Check collection lists",
  sync_definitions: "Sync list collections",
  push_posters: "Push posters",
  poster_set: "Import poster set",
  store_bases: "Store base posters",
  tidy_assets: "Tidy stored posters",
  tmdb_reset: "Reset posters from TMDb",
  arr_event: "Radarr/Sonarr update",
  safety_scan: "Weekly safety-net scan",
};

const FINISHED: Job["status"][] = ["succeeded", "failed", "cancelled"];

export function jobLabel(job: Pick<Job, "kind"> & { params?: Job["params"] }): string {
  const label = JOB_LABELS[job.kind] ?? job.kind;
  const event = job.params?.event as { title?: string; year?: number } | undefined;
  if (job.kind === "arr_event" && event?.title) return `${label}: ${event.title}${event.year ? ` (${event.year})` : ""}`;
  return label;
}

/**
 * Start a background job and follow it to the end. When it finishes, the given query keys
 * are refreshed so the page shows the result without a reload.
 */
export function useJobAction<T>(start: (arg: T) => Promise<{ job_id: number }>, refresh: string[]) {
  const qc = useQueryClient();
  const [jobId, setJobId] = useState<number | null>(null);
  const mutation = useMutation({ mutationFn: start, onSuccess: (r) => setJobId(r.job_id) });
  const job = useQuery({
    queryKey: ["jobs", jobId],
    queryFn: () => api.get<Job>(`/api/jobs/${jobId}`),
    enabled: jobId !== null,
    // Stop when the job ends, or when it can't be read any more (e.g. gone after a restart).
    refetchInterval: (q) => (q.state.status === "error" || (q.state.data && FINISHED.includes(q.state.data.status)) ? false : 800),
  });
  const finished = Boolean(job.data && FINISHED.includes(job.data.status)) || job.isError;
  // Refresh once per finished job (the key list is a new array on every render).
  const handled = useRef<number | null>(null);
  const keys = useRef(refresh);
  keys.current = refresh;
  useEffect(() => {
    if (!finished || jobId === null || handled.current === jobId) return;
    handled.current = jobId;
    for (const key of [...keys.current, "dashboard", "issues", "review", "changes"]) qc.invalidateQueries({ queryKey: [key] });
  }, [finished, jobId, qc]);
  return {
    run: mutation.mutate,
    running: mutation.isPending || (jobId !== null && !finished),
    job: job.data,
    error: mutation.error,
  };
}

/** Pending poster pushes, for the sidebar badge. */
export function useChangesSummary() {
  return useQuery({
    queryKey: ["changes", "summary"],
    queryFn: () => api.get<ChangesSummary>("/api/changes/summary"),
    // Jobs and poster choices invalidate it; this only catches changes made elsewhere.
    refetchInterval: 300000,
  });
}
