import { useState, type FormEvent } from "react";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/input";
import { ErrorNote } from "@/components/ui/misc";
import { Logo } from "@/components/Logo";
import { ApiError, login } from "@/lib/api";

interface Choice {
  slug: string;
  name: string;
}

export function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [choices, setChoices] = useState<Choice[] | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  async function submit(tenant?: string) {
    setBusy(true);
    setError(null);
    try {
      await login(email.trim(), password, tenant);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.code === "choose_tenant") {
        setChoices((e.detail as { tenants: Choice[] }).tenants);
      } else {
        setError(e);
      }
    } finally {
      setBusy(false);
    }
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault();
    void submit();
  }

  return (
    <main className="flex min-h-dvh items-center justify-center px-4 py-10">
      <div className="w-full max-w-sm">
        <Logo className="mb-6 size-12" />
        <h1 className="text-2xl font-semibold tracking-tight">Sign in</h1>
        <p className="mt-1 text-sm text-muted">Orders, chats and customers from your WhatsApp agent.</p>
        {choices ? (
          <div className="mt-6 space-y-2">
            <p className="text-sm text-ink-2">This login belongs to more than one business. Which one?</p>
            {choices.map((c) => (
              <Button key={c.slug} variant="secondary" className="w-full justify-start" disabled={busy} onClick={() => void submit(c.slug)}>
                {c.name}
              </Button>
            ))}
            <Button variant="ghost" className="w-full" onClick={() => setChoices(null)}>
              Back
            </Button>
          </div>
        ) : (
          <form onSubmit={onSubmit} className="mt-6 space-y-4">
            <Field label="Email">
              <Input
                type="email"
                autoComplete="username"
                inputMode="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </Field>
            <Field label="Password">
              <Input
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
            <ErrorNote error={error} />
            <Button type="submit" size="lg" className="w-full" disabled={busy}>
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        )}
      </div>
    </main>
  );
}
