import { useState, useCallback, useEffect } from "react";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { sendMessage as tauriSend, getMessages as tauriGetMessages } from "../lib/tauri";
import type { Conversation } from "../types";

interface ChatToken { conversation_id: string; token: string; index: number; }
interface ChatDone { conversation_id: string; message_id: string; full_text: string; }

export interface ChatMessage { id: string; role: "user" | "assistant"; content: string; isStreaming: boolean; }

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [modelId, setModelId] = useState("local/default");
  const [unlisten, setUnlisten] = useState<UnlistenFn | null>(null);

  useEffect(() => { return () => { if (unlisten) unlisten(); }; }, [unlisten]);

  const loadMessages = useCallback(async (convId: string) => {
    try {
      const msgs = await tauriGetMessages(convId);
      setMessages(msgs.map(m => ({ id: m.id, role: m.role as "user" | "assistant", content: m.content, isStreaming: false })));
    } catch { setMessages([]); }
  }, []);

  const switchConversation = useCallback(async (conv: Conversation) => {
    if (unlisten) unlisten();
    setConversationId(conv.id);
    setModelId(conv.model_id || "local/default");
    await loadMessages(conv.id);
  }, [unlisten, loadMessages]);

  const newConversation = useCallback(() => {
    if (unlisten) unlisten();
    setConversationId(null); setMessages([]); setIsLoading(false);
  }, [unlisten]);

  const sendMessage = useCallback(async (text: string) => {
    if (!text.trim()) return;
    const userMsg: ChatMessage = { id: crypto.randomUUID(), role: "user", content: text, isStreaming: false };
    const assistantMsg: ChatMessage = { id: crypto.randomUUID(), role: "assistant", content: "", isStreaming: true };
    setMessages(prev => [...prev, userMsg, assistantMsg]);
    setIsLoading(true);

    try {
      if (unlisten) unlisten();
      const u1 = await listen<ChatToken>("chat:token", event => {
        setMessages(prev => { const next = [...prev]; const last = next[next.length - 1]; if (last?.isStreaming) last.content += event.payload.token; return [...next]; });
      });
      const u2 = await listen<ChatDone>("chat:done", event => {
        setMessages(prev => { const next = [...prev]; const last = next[next.length - 1]; if (last?.isStreaming) { last.content = event.payload.full_text; last.isStreaming = false; } return [...next]; });
        setIsLoading(false);
      });
      setUnlisten(() => { u1(); u2(); });
      const conv = await tauriSend(text, conversationId ?? undefined, modelId);
      setConversationId(conv.id);
    } catch (err) {
      setMessages(prev => { const next = [...prev]; const last = next[next.length - 1]; if (last?.isStreaming) { last.content = "Error: " + String(err); last.isStreaming = false; } return [...next]; });
      setIsLoading(false);
    }
  }, [conversationId, modelId, unlisten]);

  return { messages, isLoading, sendMessage, conversationId, modelId, setModelId, switchConversation, newConversation, loadMessages };
}