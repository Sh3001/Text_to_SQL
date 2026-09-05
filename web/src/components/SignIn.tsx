import { useEffect, useState } from "react";
import { authConfig, login, signup } from "../auth";
import type { AuthConfig, User } from "../types";

// An account is identified by an email address or a phone number, so the
// single field takes either. The server normalises phone numbers, which
// is why "+91 98765 43210" and "09876543210" reach the same account.
type Mode = "signin" | "signup";

export function SignIn({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [config, setConfig] = useState<AuthConfig | null>(null);
  const [mode, setMode] = useState<Mode>("signin");
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    authConfig()
      .then((c) => {
        setConfig(c);
        // A fresh instance has nothing to sign in to, so open on the
        // account-creation form rather than one nobody can satisfy.
        if (c.setup_required) setMode("signup");
      })
      .catch((err) => {
        setConfig({ setup_required: false, signup_enabled: false, min_password_length: 8 });
        setError(err instanceof Error ? err.message : String(err));
      });
  }, []);

  function switchMode(next: Mode) {
    setMode(next);
    setError(null);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const user =
        mode === "signup"
          ? await signup(identifier, password, displayName)
          : await login(identifier, password);
      onSignedIn(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (config === null) {
    return (
      <div className="signin-screen">
        <div className="signin-card">
          <p className="signin-checking">Connecting…</p>
        </div>
      </div>
    );
  }

  const isSignup = mode === "signup";
  const firstRun = config.setup_required;
  const canSwitch = config.signup_enabled && !firstRun;

  return (
    <div className="signin-screen">
      <div className="signin-card">
        <div className="signin-brand">
          <span className="signin-mark" aria-hidden="true" />
          <div>
            <h1>Query Warden</h1>
            <p>Ask the marketplace warehouse anything.</p>
          </div>
        </div>

        {canSwitch && (
          <div className="signin-tabs" role="tablist">
            <button
              role="tab" type="button"
              aria-selected={!isSignup}
              className={!isSignup ? "active" : ""}
              onClick={() => switchMode("signin")}
            >
              Sign in
            </button>
            <button
              role="tab" type="button"
              aria-selected={isSignup}
              className={isSignup ? "active" : ""}
              onClick={() => switchMode("signup")}
            >
              Create account
            </button>
          </div>
        )}

        {firstRun && (
          <p className="signin-setup-note">
            No accounts yet. The first one you create is an operator, so it can also read the
            activity log and add teammates.
          </p>
        )}

        <form onSubmit={handleSubmit} className="signin-form">
          {isSignup && (
            <label>
              <span>Your name</span>
              <input
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
                placeholder="Optional"
                autoComplete="name"
              />
            </label>
          )}

          <label>
            <span>Email or phone number</span>
            <input
              value={identifier}
              onChange={(e) => setIdentifier(e.target.value)}
              required
              autoFocus
              autoComplete="username"
              placeholder="you@company.com or +91 98765 43210"
            />
          </label>

          <label>
            <span>Password</span>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={isSignup ? config.min_password_length : undefined}
              autoComplete={isSignup ? "new-password" : "current-password"}
              placeholder={isSignup ? `At least ${config.min_password_length} characters` : "••••••••"}
            />
          </label>

          {error && <p className="signin-error">{error}</p>}

          <button type="submit" className="signin-submit" disabled={busy}>
            {busy ? "Working…" : isSignup ? "Create account" : "Sign in"}
          </button>
        </form>

        {canSwitch && (
          <p className="signin-switch">
            {isSignup ? "Already have an account?" : "Don't have an account?"}{" "}
            <button type="button" onClick={() => switchMode(isSignup ? "signin" : "signup")}>
              {isSignup ? "Sign in" : "Create one"}
            </button>
          </p>
        )}

        {!config.signup_enabled && !firstRun && (
          <p className="signin-closed-note">
            Sign-up is closed on this instance. Ask an operator to create your account.
          </p>
        )}
      </div>
    </div>
  );
}
