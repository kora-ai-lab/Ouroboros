import type { FC, KeyboardEvent } from "react";
import { COPY } from "../../lib/copy";

interface InputBarProps {
  value: string;
  onChange: (val: string) => void;
  onSubmit: () => void;
  isLoading: boolean;
}

export const InputBar: FC<InputBarProps> = ({
  value,
  onChange,
  onSubmit,
  isLoading,
}) => {
  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (value.trim() && !isLoading) onSubmit();
    }
    if (e.key === "Escape") {
      onChange("");
      (e.target as HTMLInputElement).blur();
    }
  };

  return (
    <div data-testid="input-bar" className="flex items-center gap-3 bg-neutral-800 rounded-lg px-4 py-3 shadow-level-1">
      <input
        data-testid="chat-input"
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder={COPY.chat.placeholder}
        aria-label={COPY.chat.placeholder}
        disabled={isLoading}
        autoFocus
        className="flex-1 bg-transparent border-none outline-none text-body text-neutral-100
          placeholder:text-neutral-600 font-sans
          focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none"
      />
      <button
        data-testid="send-button"
        onClick={onSubmit}
        disabled={!value.trim() || isLoading}
        aria-label="Send message"
        className="rounded-sm px-3 py-1.5 text-label bg-brand-500 text-neutral-950
          hover:bg-brand-600 active:bg-brand-700
          disabled:opacity-40 disabled:cursor-not-allowed
          transition-colors font-medium
          focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none"
        style={{
          transitionDuration: "var(--duration-fast)",
          transitionTimingFunction: "var(--ease-default)",
        }}
      >
        Send
      </button>
    </div>
  );
};