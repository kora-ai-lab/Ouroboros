import { useState, useEffect, type FC } from "react";
import {
  getConversations,
  deleteConversation as tauriDeleteConversation,
} from "../../lib/tauri";
import type { Conversation } from "../../types";

interface Props {
  isOpen: boolean;
  onSelectConversation: (conv: Conversation) => void;
  onNewConversation: () => void;
  activeConversationId: string | null;
  refreshKey: number;
}

export const Sidebar: FC<Props> = ({
  isOpen, onSelectConversation, onNewConversation, activeConversationId, refreshKey,
}) => {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      try {
        const convs = await getConversations();
        if (!cancelled) setConversations(convs);
      } catch {
        if (!cancelled) setConversations([]);
      }
      if (!cancelled) setLoading(false);
    }
    load();
    return () => { cancelled = true; };
  }, [refreshKey]);

  async function handleDelete(id: string, e: React.MouseEvent) {
    e.stopPropagation();
    try {
      await tauriDeleteConversation(id);
      setConversations((prev) => prev.filter((c) => c.id !== id));
    } catch { /* ignore */ }
  }

  function formatDate(iso: string): string {
    try {
      const d = new Date(iso);
      const now = new Date();
      const diff = now.getTime() - d.getTime();
      if (diff < 86400000) {
        return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      }
      if (diff < 604800000) {
        return d.toLocaleDateString([], { weekday: "short" });
      }
      return d.toLocaleDateString([], { month: "short", day: "numeric" });
    } catch {
      return "";
    }
  }

  return (
    <div className={"h-full flex flex-shrink-0 transition-all " + (isOpen ? "w-60" : "w-0")}>
      <div className={"h-full flex flex-col border-r border-white/5 bg-neutral-900/90 overflow-hidden " + (isOpen ? "w-60" : "w-0")} role="region" aria-label="Conversations">
        <div className="flex items-center justify-between px-3 py-3 border-b border-white/5">
          <span className="text-label font-medium text-neutral-300">Conversations</span>
          <button
            onClick={onNewConversation}
            className="text-neutral-500 hover:text-neutral-300 text-label-sm px-2 py-1 rounded-sm hover:bg-neutral-800 transition-colors focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none"
            title="New conversation"
          >
            + New
          </button>
        </div>

        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="p-3 space-y-2">
              {[1, 2, 3].map((i) => (
                <div key={i} className="h-5 bg-neutral-800 rounded-sm animate-pulse" />
              ))}
            </div>
          )}

          {!loading && conversations.length === 0 && (
            <div className="p-4 text-center text-label-sm text-neutral-500">
              No conversations yet.
            </div>
          )}

          {!loading &&
            conversations.map((conv) => (
              <div
                key={conv.id}
                onClick={() => onSelectConversation(conv)}
                className={"px-3 py-2.5 cursor-pointer group transition-colors border-l-2 " +
                  (conv.id === activeConversationId
                    ? "border-brand-400 bg-neutral-800"
                    : "border-transparent hover:bg-neutral-800/50")
                }
              >
                <div className="flex items-center justify-between">
                  <span className="text-label-sm text-neutral-300 truncate flex-1">
                    {conv.title || "New conversation"}
                  </span>
                  <button
                    onClick={(e) => handleDelete(conv.id, e)}
                    className="ml-2 text-neutral-600 hover:text-error-500 opacity-0 group-hover:opacity-100 transition-all text-xs px-1"
                    title="Delete"
                  >
                    x
                  </button>
                </div>
                <div className="text-xs text-neutral-600 mt-0.5">
                  {formatDate(conv.updated_at)}
                </div>
              </div>
            ))}
        </div>
      </div>
    </div>
  );
};