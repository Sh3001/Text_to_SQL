import { useState } from "react";
import type { Conversation, User } from "../types";

function relativeTime(iso: string): string {
  // The API sends Postgres timestamptz as a string with a space rather
  // than a "T"; Safari refuses that, so normalise before parsing.
  const then = new Date(iso.replace(" ", "T")).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return days < 7 ? `${days}d ago` : new Date(then).toLocaleDateString();
}

export function Sidebar({
  conversations,
  activeId,
  user,
  collapsed,
  onSelect,
  onNew,
  onDelete,
  onSignOut,
  onToggle,
}: {
  conversations: Conversation[];
  activeId: string | null;
  user: User;
  collapsed: boolean;
  onSelect: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void;
  onSignOut: () => void;
  onToggle: () => void;
}) {
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  if (collapsed) {
    return (
      <aside className="sidebar sidebar-collapsed">
        <button className="icon-btn" onClick={onToggle} aria-label="Show conversations" title="Show conversations">
          ☰
        </button>
        <button className="icon-btn" onClick={onNew} aria-label="New conversation" title="New conversation">
          +
        </button>
      </aside>
    );
  }

  return (
    <aside className="sidebar">
      <div className="sidebar-top">
        <button className="icon-btn" onClick={onToggle} aria-label="Hide conversations" title="Hide conversations">
          ☰
        </button>
        <button className="new-chat-btn" onClick={onNew}>
          <span aria-hidden="true">+</span> New chat
        </button>
      </div>

      <nav className="convo-list" aria-label="Conversations">
        {conversations.length === 0 && (
          <p className="convo-empty">Your conversations will appear here.</p>
        )}
        {conversations.map((c) => (
          <div key={c.id} className={`convo-item ${c.id === activeId ? "active" : ""}`}>
            <button className="convo-open" onClick={() => onSelect(c.id)} title={c.title}>
              <span className="convo-title">{c.title}</span>
              <span className="convo-meta">
                {relativeTime(c.updated_at)}
                {c.message_count > 0 && ` · ${Math.floor(c.message_count / 2)} Q`}
              </span>
            </button>
            {confirmingId === c.id ? (
              <span className="convo-confirm">
                <button
                  className="convo-confirm-yes"
                  onClick={() => {
                    onDelete(c.id);
                    setConfirmingId(null);
                  }}
                >
                  Delete
                </button>
                <button className="convo-confirm-no" onClick={() => setConfirmingId(null)}>
                  Keep
                </button>
              </span>
            ) : (
              <button
                className="convo-delete"
                onClick={() => setConfirmingId(c.id)}
                aria-label={`Delete ${c.title}`}
                title="Delete"
              >
                ×
              </button>
            )}
          </div>
        ))}
      </nav>

      <div className="sidebar-user">
        <div className="user-line">
          <span className="user-avatar" aria-hidden="true">
            {(user.display_name || user.email).charAt(0).toUpperCase()}
          </span>
          <span className="user-text">
            <span className="user-name">{user.display_name || user.email}</span>
            <span className="user-role">
              {user.role === "operator" ? "Operator" : "Member"} · tenant {user.tenant_id}
            </span>
          </span>
        </div>
        <button className="signout-btn" onClick={onSignOut}>
          Sign out
        </button>
      </div>
    </aside>
  );
}
