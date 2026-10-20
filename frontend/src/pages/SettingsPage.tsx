import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Pencil, Plus, Trash2, XCircle } from "lucide-react";
import { useState } from "react";
import {
  Button,
  Card,
  ErrorNote,
  Field,
  KIND_LABEL,
  Modal,
  PageHeader,
  ServerChip,
  Spinner,
  Tabs,
  formatDate,
  inputClass,
  selectClass,
} from "../components/ui";
import { api, type Server, type ServerKind, type ServerLibrary, type TestResult } from "../lib/api";
import { useLibraries, useServers } from "../lib/hooks";

type Tab = "servers" | "libraries" | "general";

export default function SettingsPage() {
  const [tab, setTab] = useState<Tab>("servers");
  return (
    <>
      <PageHeader title="Settings" />
      <Tabs<Tab>
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "servers", label: "Media servers" },
          { id: "libraries", label: "Library mapping" },
          { id: "general", label: "General" },
        ]}
      />
      {tab === "servers" && <ServersTab />}
      {tab === "libraries" && <LibrariesTab />}
      {tab === "general" && <GeneralTab />}
    </>
  );
}

// --- servers --------------------------------------------------------------------------------------

const TOKEN_HINT: Record<ServerKind, string> = {
  plex: "Your X-Plex-Token (Plex Web → any item → Get Info → View XML, it's in the URL).",
  jellyfin: "Dashboard → API Keys → create a key for Coverarr.",
  emby: "Settings → Advanced → API Keys → New API key.",
};

interface Draft {
  id?: number;
  kind: ServerKind;
  name: string;
  url: string;
  token: string;
  verify_ssl: boolean;
  enabled: boolean;
}

function ServersTab() {
  const { data: servers, isLoading } = useServers();
  const [editing, setEditing] = useState<Draft | null>(null);
  const qc = useQueryClient();
  const remove = useMutation({
    mutationFn: (id: number) => api.del(`/api/servers/${id}`),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["servers"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      qc.invalidateQueries({ queryKey: ["libraries"] });
    },
  });

  if (isLoading) return <Spinner />;
  return (
    <>
      <div className="mb-4 flex flex-wrap gap-2">
        {(["plex", "jellyfin", "emby"] as ServerKind[]).map((kind) => (
          <Button
            key={kind}
            icon={<Plus className="size-4" />}
            onClick={() =>
              setEditing({ kind, name: KIND_LABEL[kind], url: "http://", token: "", verify_ssl: true, enabled: true })
            }
          >
            Add {KIND_LABEL[kind]}
          </Button>
        ))}
      </div>
      <Card>
        {servers?.length ? (
          <ul className="divide-y divide-line">
            {servers.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                <ServerChip server={s} dim={!s.enabled} />
                <div className="min-w-0 flex-1">
                  <div className="font-medium">
                    {s.name} {!s.enabled && <span className="text-xs text-muted">(disabled)</span>}
                  </div>
                  <div className="truncate text-xs text-muted">
                    {s.url} · {s.version ? `v${s.version}` : "version unknown"} · last OK {formatDate(s.last_ok_at)}
                  </div>
                  {s.last_error && <div className="mt-1 text-xs text-danger">{s.last_error}</div>}
                </div>
                <Button
                  size="sm"
                  variant="ghost"
                  icon={<Pencil className="size-4" />}
                  onClick={() => setEditing({ id: s.id, kind: s.kind, name: s.name, url: s.url, token: "", verify_ssl: s.verify_ssl, enabled: s.enabled })}
                >
                  Edit
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  icon={<Trash2 className="size-4" />}
                  onClick={() => {
                    if (confirm(`Remove ${s.name}? Its imported data is deleted from Coverarr (nothing changes on the server).`)) remove.mutate(s.id);
                  }}
                >
                  Remove
                </Button>
              </li>
            ))}
          </ul>
        ) : (
          <div className="px-4 py-10 text-center text-sm text-muted">No servers yet. Add Plex, Jellyfin and Emby above.</div>
        )}
      </Card>
      {editing && <ServerModal draft={editing} onClose={() => setEditing(null)} />}
    </>
  );
}

function ServerModal({ draft: initial, onClose }: { draft: Draft; onClose: () => void }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState(initial);
  const [test, setTest] = useState<TestResult | null>(null);
  const [testing, setTesting] = useState(false);
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v }));

  const save = useMutation({
    mutationFn: () => {
      const body = { ...draft, token: draft.token || null };
      return draft.id ? api.put<Server>(`/api/servers/${draft.id}`, body) : api.post<Server>("/api/servers", body);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["servers"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
      onClose();
    },
  });

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      setTest(await api.post<TestResult>("/api/servers/test", { ...draft, token: draft.token || null }));
    } catch (err) {
      setTest({ ok: false, error: err instanceof Error ? err.message : String(err) });
    } finally {
      setTesting(false);
    }
  };

  return (
    <Modal
      open
      onClose={onClose}
      title={`${draft.id ? "Edit" : "Add"} ${KIND_LABEL[draft.kind]} server`}
      footer={
        <>
          <Button onClick={runTest} loading={testing}>
            Test
          </Button>
          <Button variant="primary" onClick={() => save.mutate()} loading={save.isPending}>
            Save
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label="Name">
          <input className={inputClass} value={draft.name} onChange={(e) => set("name", e.target.value)} />
        </Field>
        <Field label="URL" hint={draft.kind === "plex" ? "e.g. http://192.168.1.10:32400" : "e.g. http://192.168.1.10:8096"}>
          <input className={inputClass} value={draft.url} onChange={(e) => set("url", e.target.value)} placeholder="http://host:port" />
        </Field>
        <Field label={draft.kind === "plex" ? "Token" : "API key"} hint={draft.id ? "Leave blank to keep the saved one. " + TOKEN_HINT[draft.kind] : TOKEN_HINT[draft.kind]}>
          <input className={inputClass} type="password" value={draft.token} onChange={(e) => set("token", e.target.value)} autoComplete="off" />
        </Field>
        <div className="flex flex-wrap gap-6 text-sm">
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={draft.enabled} onChange={(e) => set("enabled", e.target.checked)} /> Enabled
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={draft.verify_ssl} onChange={(e) => set("verify_ssl", e.target.checked)} /> Verify SSL certificate
          </label>
        </div>
        {test &&
          (test.ok ? (
            <div className="rounded-md border border-ok/30 bg-ok/10 px-3 py-2 text-sm">
              <div className="flex items-center gap-2 font-medium text-ok">
                <CheckCircle2 className="size-4" /> Connected to {test.name} {test.version}
              </div>
              <div className="mt-1 text-muted">
                Libraries: {test.libraries?.map((l) => `${l.name} (${l.kind})`).join(", ") || "none found"}
              </div>
            </div>
          ) : (
            <div className="flex items-start gap-2 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
              <XCircle className="mt-0.5 size-4 shrink-0" /> {test.error}
            </div>
          ))}
        <ErrorNote error={save.error} />
      </div>
    </Modal>
  );
}

// --- library mapping ------------------------------------------------------------------------------

function LibrariesTab() {
  const { data, isLoading } = useLibraries();
  const qc = useQueryClient();
  const update = useMutation({
    mutationFn: ({ sl, body }: { sl: ServerLibrary; body: Record<string, unknown> }) =>
      api.put(`/api/server-libraries/${sl.id}`, { enabled: sl.enabled, library_id: sl.library_id, ...body }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["libraries"] });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
  if (isLoading || !data) return <Spinner />;
  const all = [...data.libraries.flatMap((l) => l.server_libraries), ...data.unmapped];
  if (all.length === 0) {
    return <div className="text-sm text-muted">Libraries appear here after the first import.</div>;
  }

  const onChange = (sl: ServerLibrary, value: string) => {
    if (value === "__new") {
      const name = prompt("Name for the new library", sl.name);
      if (name) update.mutate({ sl, body: { library_id: null, new_library_name: name } });
    } else if (value === "__none") {
      update.mutate({ sl, body: { library_id: null, enabled: false } });
    } else {
      update.mutate({ sl, body: { library_id: Number(value), enabled: true } });
    }
  };

  return (
    <>
      <p className="mb-4 max-w-3xl text-sm text-muted">
        Each Coverarr library groups the matching library from every server, so titles are matched across them. Libraries
        were paired automatically on first import; change any pairing here and Coverarr re-matches right away. Turn a
        library off to stop managing it.
      </p>
      <Card>
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase tracking-wide text-muted">
            <tr className="border-b border-line">
              <th className="px-4 py-2 font-medium">Server library</th>
              <th className="px-4 py-2 font-medium">Type</th>
              <th className="px-4 py-2 font-medium">Coverarr library</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {all.map((sl) => (
              <tr key={sl.id}>
                <td className="px-4 py-2">
                  <span className="inline-flex items-center gap-2">
                    <ServerChip server={{ kind: sl.server_kind, name: sl.server_name }} />
                    {sl.server_name} / <span className="font-medium">{sl.name}</span>
                  </span>
                </td>
                <td className="px-4 py-2 text-muted">{sl.kind === "movie" ? "Movies" : "TV"}</td>
                <td className="px-4 py-2">
                  <select
                    className={selectClass + " h-9 w-64"}
                    value={sl.enabled && sl.library_id ? String(sl.library_id) : "__none"}
                    onChange={(e) => onChange(sl, e.target.value)}
                    disabled={update.isPending}
                  >
                    {data.libraries
                      .filter((l) => l.kind === sl.kind)
                      .map((l) => (
                        <option key={l.id} value={l.id}>
                          {l.name}
                        </option>
                      ))}
                    <option value="__new">New library…</option>
                    <option value="__none">Don't manage</option>
                  </select>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>
      <ErrorNote error={update.error} />
    </>
  );
}

// --- general ----------------------------------------------------------------------------------------

interface SettingsData {
  tmdb_api_key: { has_value: boolean };
  trakt_client_id: string | null;
  trakt_client_secret: { has_value: boolean };
  poster_match_threshold: number;
  server_load: "gentle" | "normal" | "fast";
}

function GeneralTab() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["settings"], queryFn: () => api.get<SettingsData>("/api/settings") });
  const [form, setForm] = useState<Record<string, string>>({});
  const save = useMutation({
    mutationFn: () => {
      const body: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(form)) body[k] = v;
      return api.put("/api/settings", body);
    },
    onSuccess: () => {
      setForm({});
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });
  if (isLoading || !data) return <Spinner />;
  const val = (k: string, fallback: string) => form[k] ?? fallback;
  const set = (k: string, v: string) => setForm((f) => ({ ...f, [k]: v }));

  return (
    <Card className="max-w-2xl space-y-5 p-5">
      <Field label="TMDb API key" hint={data.tmdb_api_key.has_value ? "Saved. Enter a new key to replace it." : "Used for poster choices and TMDb list collections (coming in later phases)."}>
        <input className={inputClass} type="password" value={val("tmdb_api_key", "")} onChange={(e) => set("tmdb_api_key", e.target.value)} autoComplete="off" />
      </Field>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Trakt client ID">
          <input className={inputClass} value={val("trakt_client_id", data.trakt_client_id ?? "")} onChange={(e) => set("trakt_client_id", e.target.value)} />
        </Field>
        <Field label="Trakt client secret" hint={data.trakt_client_secret.has_value ? "Saved." : undefined}>
          <input className={inputClass} type="password" value={val("trakt_client_secret", "")} onChange={(e) => set("trakt_client_secret", e.target.value)} autoComplete="off" />
        </Field>
      </div>
      <Field
        label="Server load"
        hint="How hard Coverarr works your media servers during imports and poster scans. Normal imports all servers at once; gentle goes one at a time with fewer parallel requests, useful if someone is streaming; fast suits capable servers."
      >
        <select className={selectClass} value={val("server_load", data.server_load)} onChange={(e) => set("server_load", e.target.value)}>
          <option value="gentle">Gentle</option>
          <option value="normal">Normal</option>
          <option value="fast">Fast</option>
        </select>
      </Field>
      <Field
        label="Poster match sensitivity"
        hint="How different two posters can look (0–32) and still count as the same artwork. Higher ignores more change; 10 suits overlays like Kometa's."
      >
        <input
          className={inputClass + " max-w-32"}
          type="number"
          min={0}
          max={32}
          value={val("poster_match_threshold", String(data.poster_match_threshold))}
          onChange={(e) => set("poster_match_threshold", e.target.value)}
        />
      </Field>
      <ErrorNote error={save.error} />
      <Button variant="primary" onClick={() => save.mutate()} loading={save.isPending} disabled={!Object.keys(form).length}>
        Save
      </Button>
    </Card>
  );
}
