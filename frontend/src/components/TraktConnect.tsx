import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, LogIn, LogOut } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { Button, Card, ErrorNote, Field, Spinner, formatDate, inputClass } from "./ui";

interface TraktStatus {
  client_id: boolean;
  connected: boolean;
  username: string | null;
  expires_at: number | null;
  redirect_uri: string | null;
  pending: boolean;
}

export const CALLBACK_PATH = "/trakt/callback";

/** Sign in to Trakt with PKCE (no client secret), for private lists. */
export default function TraktConnect() {
  const qc = useQueryClient();
  const { data, isLoading } = useQuery({ queryKey: ["trakt"], queryFn: () => api.get<TraktStatus>("/api/trakt") });
  const [redirect, setRedirect] = useState("");
  const [pasted, setPasted] = useState("");
  useEffect(() => {
    if (data && !redirect) setRedirect(data.redirect_uri ?? `${window.location.origin}${CALLBACK_PATH}`);
  }, [data, redirect]);
  const connect = useMutation({
    mutationFn: () => api.post<{ url: string }>("/api/trakt/connect", { redirect_uri: redirect }),
    onSuccess: ({ url }) => {
      window.location.href = url;
    },
  });
  const finish = useMutation({
    mutationFn: () => api.post<TraktStatus>("/api/trakt/callback", { response: pasted }),
    onSuccess: (s) => {
      qc.setQueryData(["trakt"], s);
      setPasted("");
    },
  });
  const disconnect = useMutation({
    mutationFn: () => api.del<TraktStatus>("/api/trakt"),
    onSuccess: (s) => qc.setQueryData(["trakt"], s),
  });
  if (isLoading || !data) return <Spinner />;

  return (
    <Card className="max-w-2xl space-y-4 p-5">
      <h2 className="font-semibold">Trakt account</h2>
      <p className="text-sm text-muted">
        Public Trakt lists and charts only need the client ID above. Sign in to read your private lists. Coverarr signs in with PKCE,
        so no client secret is needed; the sign-in renews itself every week.
      </p>
      {data.connected ? (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-ok/30 bg-ok/10 px-3 py-2 text-sm">
          <span className="flex items-center gap-2">
            <CheckCircle2 className="size-4 text-ok" />
            Signed in{data.username ? ` as ${data.username}` : ""}
            {data.expires_at && <span className="text-muted">· renews before {formatDate(new Date(data.expires_at * 1000).toISOString())}</span>}
          </span>
          <Button size="sm" variant="ghost" icon={<LogOut className="size-3.5" />} loading={disconnect.isPending} onClick={() => disconnect.mutate()}>
            Sign out
          </Button>
        </div>
      ) : (
        <>
          <Field
            label="Redirect URI"
            hint={
              <>
                Set exactly this as the redirect URI of your app at{" "}
                <a href="https://trakt.tv/oauth/applications" target="_blank" rel="noreferrer" className="text-accent hover:underline">
                  trakt.tv/oauth/applications
                </a>
                . Trakt wants an https address; if Coverarr isn't on https, register any https address there, use it here, and paste back
                the address the browser lands on (it doesn't need to load).
              </>
            }
          >
            <input className={inputClass + " font-mono text-xs"} value={redirect} onChange={(e) => setRedirect(e.target.value)} />
          </Field>
          <Button variant="primary" icon={<LogIn className="size-4" />} disabled={!data.client_id} loading={connect.isPending} onClick={() => connect.mutate()}>
            Connect Trakt
          </Button>
          {!data.client_id && <span className="ml-3 text-sm text-muted">Save the client ID first.</span>}
          {data.pending && (
            <Field label="Back from Trakt?" hint="If you weren't brought back here automatically, paste the address you landed on after allowing Coverarr (or just the code).">
              <div className="flex gap-2">
                <input className={inputClass} value={pasted} onChange={(e) => setPasted(e.target.value)} placeholder="https://…?code=…&state=…" />
                <Button disabled={!pasted.trim()} loading={finish.isPending} onClick={() => finish.mutate()}>
                  Finish
                </Button>
              </div>
            </Field>
          )}
        </>
      )}
      <ErrorNote error={connect.error ?? finish.error ?? disconnect.error} />
    </Card>
  );
}
