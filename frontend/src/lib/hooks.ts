import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api, type Dashboard, type Job, type Library, type Server } from "./api";

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
      for (const key of ["items", "item", "collections", "collection", "review", "issues", "libraries", "jobs", "servers"]) {
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
};

export function jobLabel(job: Pick<Job, "kind">): string {
  return JOB_LABELS[job.kind] ?? job.kind;
}
