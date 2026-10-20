import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { Button, ErrorNote, Field, inputClass } from "../components/ui";
import { api } from "../lib/api";

export default function AuthPage({ setup }: { setup: boolean }) {
  const qc = useQueryClient();
  const [username, setUsername] = useState(setup ? "admin" : "");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (setup && password !== confirm) {
      setError(new Error("Passwords don't match"));
      return;
    }
    setBusy(true);
    try {
      await api.post(setup ? "/api/auth/setup" : "/api/auth/login", { username, password });
      await qc.invalidateQueries({ queryKey: ["auth"] });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <form onSubmit={submit} className="w-full max-w-sm space-y-4 rounded-lg border border-line bg-panel p-6">
        <div className="flex items-center gap-3">
          <img src="/favicon.svg" alt="" className="size-9" />
          <div>
            <h1 className="text-xl font-bold">Coverarr</h1>
            <p className="text-sm text-muted">{setup ? "Create the admin account" : "Sign in"}</p>
          </div>
        </div>
        <Field label="Username">
          <input className={inputClass} value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
        </Field>
        <Field label="Password" hint={setup ? "At least 8 characters" : undefined}>
          <input
            className={inputClass}
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete={setup ? "new-password" : "current-password"}
            minLength={setup ? 8 : 1}
            required
          />
        </Field>
        {setup && (
          <Field label="Confirm password">
            <input className={inputClass} type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" required />
          </Field>
        )}
        <ErrorNote error={error} />
        <Button type="submit" variant="primary" className="w-full" loading={busy}>
          {setup ? "Create account" : "Sign in"}
        </Button>
      </form>
    </div>
  );
}
