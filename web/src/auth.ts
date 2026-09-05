// Token storage and the auth calls. The token lives in localStorage:
// adequate for a tool behind a login on a trusted device, and the honest
// tradeoff is that it's readable by any script on the origin. A
// httpOnly cookie would be stronger; it needs same-origin serving or a
// CSRF token, neither of which this deployment has yet.

import type { AuthConfig, TokenResponse, User } from "./types";

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8001";
const TOKEN_KEY = "qw.token";
const USER_KEY = "qw.user";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function getStoredUser(): User | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as User) : null;
  } catch {
    return null;
  }
}

function store(token: string, user: User) {
  try {
    localStorage.setItem(TOKEN_KEY, token);
    localStorage.setItem(USER_KEY, JSON.stringify(user));
  } catch {
    /* private mode — the session still works, it just won't survive a reload */
  }
}

export function clearSession() {
  try {
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(USER_KEY);
  } catch {
    /* nothing to clear */
  }
}

export function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

/** Throws with the server's own message so the form can show why it failed. */
async function postJSON<T>(path: string, body: unknown): Promise<T> {
  const resp = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  const text = await resp.text();
  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const parsed = JSON.parse(text);
      if (typeof parsed.detail === "string") detail = parsed.detail;
      else if (Array.isArray(parsed.detail)) detail = parsed.detail[0]?.msg ?? detail;
    } catch {
      /* server sent something that isn't JSON — keep the status line */
    }
    throw new Error(detail);
  }
  return JSON.parse(text) as T;
}

export async function login(email: string, password: string): Promise<User> {
  const res = await postJSON<TokenResponse>("/api/auth/login", { email, password });
  store(res.access_token, res.user);
  return res.user;
}

/** Self-serve account creation. Role and tenant are decided by the
 *  server — deliberately not parameters here. */
export async function signup(
  email: string,
  password: string,
  displayName?: string
): Promise<User> {
  const res = await postJSON<TokenResponse>("/api/auth/signup", {
    email,
    password,
    display_name: displayName || null,
  });
  store(res.access_token, res.user);
  return res.user;
}

export async function authConfig(): Promise<AuthConfig> {
  const resp = await fetch(`${API_URL}/api/auth/config`);
  if (!resp.ok) throw new Error(`API unreachable (${resp.status})`);
  return resp.json();
}

/** Confirms a stored token is still valid; null means sign in again. */
export async function fetchMe(): Promise<User | null> {
  if (!getToken()) return null;
  const resp = await fetch(`${API_URL}/api/auth/me`, { headers: authHeaders() });
  if (resp.status === 401) {
    clearSession();
    return null;
  }
  if (!resp.ok) throw new Error(`could not verify session (${resp.status})`);
  return resp.json();
}
