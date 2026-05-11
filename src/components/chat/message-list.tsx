import { useEffect, useRef, type FC } from "react";
import { MessageBubble } from "./message-bubble";
import { COPY } from "../../lib/copy";
import type { ChatMessage } from "../../hooks/useChat";

interface MessageListProps {
  messages: ChatMessage[];
  isLoading: boolean;
}

export const MessageList: FC<MessageListProps> = ({ messages, isLoading }) => {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  if (messages.length === 0 && !isLoading) {
    return (
      <div
        data-testid="empty-state"
        className="flex flex-col items-center justify-center h-full gap-3 px-4 text-center"
      >
        <p className="text-h3 font-heading text-neutral-200">
          {COPY.chat.empty.heading}
        </p>
        <p className="text-body text-neutral-400 max-w-md">
          {COPY.chat.empty.description}
        </p>
        <div className="flex flex-wrap gap-2 mt-2 justify-center">
          {COPY.chat.suggestions.map((s, i) => (
            <span
              key={i}
              className="text-label-sm text-neutral-400 bg-neutral-800 rounded-full px-3 py-1 border border-white/5"
            >
              {s}
            </span>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div data-testid="message-list" className="flex flex-col gap-4 p-4 flex-1 overflow-y-auto" role="log" aria-live="polite">
      {messages.map((msg) => (
        <MessageBubble key={msg.id} message={msg} />
      ))}
      {isLoading && messages.length > 0 && messages[messages.length - 1].isStreaming && (
        <div data-testid="skeleton" className="flex items-center gap-2 text-label-sm text-neutral-500 pl-1">
          <span className="animate-pulse">{COPY.chat.loading.thinking}</span>
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  );
};
