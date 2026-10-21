import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarClock, Copy, ListOrdered, RefreshCw, Webhook } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useJobAction } from "../lib/hooks";
import CancelJob from "./CancelJob";
import { Button, Card, ErrorNote, Field, Spinner, formatDate, inputClass, selectClass } from "./ui";

interface Automation {
  scan_servers: boolean;
  wait_seconds: number;
  new_base: "tmdb" | "none";
  auto_push: boolean;
  weekly_scan: { enabled: boolean; weekday: number; hour: number };
  collections_sync: { enabled: boolean; hour: number };
  webhook_path: string;
  weekly_last_run: string | null;
  weekly_next_run: string | null;
}

const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

/** Radarr/Sonarr webhook and the weekly safety-net scan. */
export default function AutomationSettings() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["automation"], queryFn: () => api.get<Automation>("/api/automation") });
  const [form, setForm] = useState<Automation | null>(null);
  useEffect(() => {
    if (data) setForm(data);
  }, [data]);
  const save = useMutation({
    mutationFn: (body: Automation) => api.put<Automation>("/api/automation", body),
    onSuccess: (saved) => qc.setQueryData(["automation"], saved),
  });
  const rotate = useMutation({
    mutationFn: () => api.post<Automation>("/api/automation/webhook-token"),
    onSuccess: (saved) => qc.setQueryData(["automation"], saved),
  });
  const scan = useJobAction(() => api.post<{ job_id: number }>("/api/automation/scan-now"), ["automation", "items", "collections"]);
  const [copied, setCopied] = useState(false);
  if (isLoading || !data || !form) return <Spinner />;

  const url = `${window.location.origin}${data.webhook_path}`;
  const set = (patch: Partial<Automation>) => setForm({ ...form, ...patch });
  const setWeekly = (patch: Partial<Automation["weekly_scan"]>) => set({ weekly_scan: { ...form.weekly_scan, ...patch } });
  const setDaily = (patch: Partial<Automation["collections_sync"]>) => set({ collections_sync: { ...form.collections_sync, ...patch } });
  const dirty = JSON.stringify(form) !== JSON.stringify(data);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* the field can still be selected and copied by hand */
    }
  };

  return (
    <div className="max-w-3xl space-y-4">
      <Card className="space-y-4 p-5">
        <h2 className="flex items-center gap-2 font-semibold">
          <Webhook className="size-4" /> Radarr and Sonarr
        </h2>
        <p className="text-sm text-muted">
          When Radarr or Sonarr imports, upgrades, renames or deletes a file, Coverarr refreshes just that title on your servers
          instead of a full import, gives new titles a base poster and pushes it (with overlays, if they're on).
        </p>
        <Field
          label="Webhook URL"
          hint={
            <>
              In Radarr and Sonarr: <em>Settings → Connect → + → Webhook</em>, method POST, with <em>On Import</em>, <em>On Upgrade</em>,{" "}
              <em>On Rename</em> and the delete events ticked. Use an address Radarr/Sonarr can reach (e.g. the container name on the same
              Docker network). The URL contains a secret: anyone with it can trigger refreshes.
            </>
          }
        >
          <div className="flex gap-2">
            <input className={inputClass + " font-mono text-xs"} readOnly value={url} onFocus={(e) => e.target.select()} />
            <Button icon={<Copy className="size-4" />} onClick={copy}>
              {copied ? "Copied" : "Copy"}
            </Button>
          </div>
        </Field>
        <Button
          size="sm"
          variant="ghost"
          icon={<RefreshCw className="size-3.5" />}
          loading={rotate.isPending}
          onClick={() => confirm("Make a new webhook URL? The current one stops working until you update Radarr and Sonarr.") && rotate.mutate()}
        >
          New URL
        </Button>

        <div className="grid gap-4 sm:grid-cols-2">
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1" checked={form.scan_servers} onChange={(e) => set({ scan_servers: e.target.checked })} />
            <span>
              Ask the servers to scan the folder
              <span className="block text-xs text-muted">Not needed if Radarr/Sonarr already tell Plex, or your servers watch the folders.</span>
            </span>
          </label>
          <label className="flex items-start gap-2 text-sm">
            <input type="checkbox" className="mt-1" checked={form.auto_push} onChange={(e) => set({ auto_push: e.target.checked })} />
            <span>
              Push the title's posters straight away
              <span className="block text-xs text-muted">Otherwise they wait in Changes.</span>
            </span>
          </label>
          <Field label="Wait before looking (seconds)" hint="Time for the servers to scan. Servers that don't have the file yet are tried again after 1, 2, 5 and 15 minutes.">
            <input
              className={inputClass + " max-w-32"}
              type="number"
              min={0}
              max={3600}
              value={form.wait_seconds}
              onChange={(e) => set({ wait_seconds: Number(e.target.value) })}
            />
          </Field>
          <Field label="Base poster for new titles" hint="Seasons of a new show get theirs too. Needs a TMDb API key (General tab).">
            <select className={selectClass} value={form.new_base} onChange={(e) => set({ new_base: e.target.value as Automation["new_base"] })}>
              <option value="tmdb">TMDb's main poster</option>
              <option value="none">None (choose it yourself)</option>
            </select>
          </Field>
        </div>
      </Card>

      <Card className="space-y-4 p-5">
        <h2 className="flex items-center gap-2 font-semibold">
          <CalendarClock className="size-4" /> Weekly safety-net scan
        </h2>
        <p className="text-sm text-muted">
          Catches anything a webhook missed: a full import, fingerprints for changed posters, then a push of only the posters that really
          differ from their base. If Coverarr is down at the scheduled time, it runs once it's back.
        </p>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={form.weekly_scan.enabled} onChange={(e) => setWeekly({ enabled: e.target.checked })} /> Run every week
        </label>
        <div className="flex flex-wrap gap-3">
          <select className={selectClass} value={form.weekly_scan.weekday} disabled={!form.weekly_scan.enabled} onChange={(e) => setWeekly({ weekday: Number(e.target.value) })}>
            {DAYS.map((d, i) => (
              <option key={d} value={i}>
                {d}
              </option>
            ))}
          </select>
          <select className={selectClass} value={form.weekly_scan.hour} disabled={!form.weekly_scan.enabled} onChange={(e) => setWeekly({ hour: Number(e.target.value) })}>
            {Array.from({ length: 24 }, (_, h) => (
              <option key={h} value={h}>
                {String(h).padStart(2, "0")}:00
              </option>
            ))}
          </select>
        </div>
        <p className="text-xs text-muted">
          Times are the server's local time (set <code>TZ</code> on the container).
          {data.weekly_next_run && <> Next run: {formatDate(data.weekly_next_run)}.</>}
          {data.weekly_last_run && <> Last run: {formatDate(data.weekly_last_run)}.</>}
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <Button icon={<RefreshCw className="size-4" />} loading={scan.running} onClick={() => scan.run(undefined)}>
            Run the scan now
          </Button>
          <CancelJob job={scan.job} />
          {scan.job && (scan.job.status === "queued" || scan.job.status === "running") && <span className="text-sm text-muted">{scan.job.message ?? "Queued"}</span>}
          {scan.job?.status === "succeeded" && (
            <span className="text-sm text-muted">
              Done: {Number(scan.job.result?.push?.uploaded ?? 0).toLocaleString()} posters pushed.{" "}
              <Link to="/jobs" className="text-accent hover:underline">
                Details
              </Link>
            </span>
          )}
          {scan.job?.status === "failed" && <span className="text-sm text-danger">{scan.job.error}</span>}
        </div>
      </Card>

      <Card className="space-y-3 p-5">
        <h2 className="flex items-center gap-2 font-semibold">
          <ListOrdered className="size-4" /> List collections
        </h2>
        <p className="text-sm text-muted">
          Every day, sync the{" "}
          <Link to="/lists" className="text-accent hover:underline">
            list collections
          </Link>{" "}
          that are switched on, so charts and lists stay current. The weekly scan syncs them too.
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={form.collections_sync.enabled} onChange={(e) => setDaily({ enabled: e.target.checked })} /> Sync daily at
          </label>
          <select className={selectClass} value={form.collections_sync.hour} disabled={!form.collections_sync.enabled} onChange={(e) => setDaily({ hour: Number(e.target.value) })}>
            {Array.from({ length: 24 }, (_, h) => (
              <option key={h} value={h}>
                {String(h).padStart(2, "0")}:00
              </option>
            ))}
          </select>
        </div>
      </Card>

      <ErrorNote error={save.error ?? rotate.error ?? scan.error} />
      <Button variant="primary" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate(form)}>
        Save
      </Button>
    </div>
  );
}
