import type { FC } from "react";

interface StreamingTextProps {
  text: string;
  isStreaming: boolean;
}

export const StreamingText: FC<StreamingTextProps> = ({ text, isStreaming }) => {
  return (
    <span data-testid="streaming-text" className="text-body text-neutral-100 whitespace-pre-wrap" aria-live="assertive">
      {text}
      {isStreaming && (
        <span className="inline-block w-1.5 h-4 bg-brand-400 ml-0.5 animate-pulse" />
      )}
    </span>
  );
};
