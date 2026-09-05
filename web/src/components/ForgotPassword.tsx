import { useState } from "react";
import { forgotPassword, resetPassword } from "../auth";
import type { AuthConfig, User } from "../types";

// Two steps, but they're separable on purpose: someone arriving from a
// reset link already has a token and should land straight on step two,
// without having to ask for a reset they already have.
type Step = "request" | "redeem";

export function ForgotPassword({
  config,
  initialToken,
  onSignedIn,
  onBack,
}: {
  config: AuthConfig;
  initialToken?: string;
  onSignedIn: (user: User) => void;
  onBack: () => void;
}) {
  const [step, setStep] = useState<Step>(initialToken ? "redeem" : "request");
  const [identifier, setIdentifier] = useState("");
  const [token, setToken] = useState(initialToken ?? "");
  const [newPassword, setNewPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [sentMessage, setSentMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleRequest(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const { message } = await forgotPassword(identifier);
      setSentMessage(message);
      setStep("redeem");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function handleRedeem(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (newPassword !== confirm) {
      setError("Those two passwords don't match.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onSignedIn(await resetPassword(token.trim(), newPassword));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  const brand = (
    <div className="signin-brand">
      <span className="signin-mark" aria-hidden="true" />
      <div>
        <h1>Query Warden</h1>
        <p>Reset your password.</p>
      </div>
    </div>
  );

  return (
    <div className="signin-screen">
      <div className="signin-card">
        {brand}

        <button className="signin-back" type="button" onClick={onBack}>
          ← Back to sign in
        </button>

        {step === "request" ? (
          <>
            <p className="signin-sent-note">
              Tell us which account, and we'll prepare a reset.
            </p>
            <form onSubmit={handleRequest} className="signin-form">
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

              {error && <p className="signin-error">{error}</p>}

              <button type="submit" className="signin-submit" disabled={busy || !identifier.trim()}>
                {busy ? "Working…" : "Request a reset"}
              </button>
            </form>
            <p className="signin-footnote">
              Already have a reset link?{" "}
              <button type="button" className="link-btn" onClick={() => setStep("redeem")}>
                Enter it here
              </button>
            </p>
          </>
        ) : (
          <>
            {sentMessage && <p className="signin-notice">{sentMessage}</p>}
            {!sentMessage && (
              <p className="signin-sent-note">
                Paste the reset link or token you were given, then choose a new password.
              </p>
            )}

            <form onSubmit={handleRedeem} className="signin-form">
              <label>
                <span>Reset token</span>
                <input
                  value={token}
                  // A pasted full link works as well as a bare token —
                  // people paste what they were sent, not the fragment
                  // of it we happen to want.
                  onChange={(e) => {
                    const raw = e.target.value.trim();
                    const match = raw.match(/reset_token=([^&\s]+)/);
                    setToken(match ? decodeURIComponent(match[1]) : raw);
                  }}
                  required
                  autoFocus={!initialToken}
                  spellCheck={false}
                  placeholder="Paste the link or token"
                />
              </label>

              <label>
                <span>New password</span>
                <input
                  type="password"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  required
                  minLength={config.min_password_length}
                  autoComplete="new-password"
                  autoFocus={Boolean(initialToken)}
                  placeholder={`At least ${config.min_password_length} characters`}
                />
              </label>

              <label>
                <span>Confirm new password</span>
                <input
                  type="password"
                  value={confirm}
                  onChange={(e) => setConfirm(e.target.value)}
                  required
                  autoComplete="new-password"
                  placeholder="Type it again"
                />
              </label>

              {error && <p className="signin-error">{error}</p>}

              <button
                type="submit"
                className="signin-submit"
                disabled={busy || !token.trim() || !newPassword}
              >
                {busy ? "Working…" : "Set new password"}
              </button>
            </form>

            <p className="signin-footnote">
              Setting a new password signs you in here and signs you out everywhere else.
            </p>
          </>
        )}
      </div>
    </div>
  );
}
