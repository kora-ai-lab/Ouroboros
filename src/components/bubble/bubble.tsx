import React, { type FC } from "react";
import { COPY } from "../../lib/copy";

interface BubbleProps {
  dragOffset: { x: number; y: number };
  onClick: () => void;
  onMouseDown: (e: React.MouseEvent) => void;
  onSettings: () => void;
}

export const Bubble: FC<BubbleProps> = ({ dragOffset, onClick, onMouseDown, onSettings }) => {
  return (
    <div
      data-testid="bubble"
      role="button"
      aria-label={COPY.bubble.ariaLabel}
      tabIndex={0}
      onClick={onClick}
      onMouseDown={onMouseDown}
      onContextMenu={(e) => {
        e.preventDefault();
        const menu = document.createElement("div");
        menu.style.cssText = "position:fixed;z-index:999999;background:#1f1c17;border:1px solid #363028;border-radius:8px;padding:4px;min-width:120px;box-shadow:0 8px 24px rgba(0,0,0,0.4)";
        menu.style.left = e.clientX + "px";
        menu.style.top = e.clientY + "px";
        const items: Array<[string, () => void]> = [["Settings", onSettings], ["Quit", () => {}]];
        items.forEach(([label, action]) => {
          const btn = document.createElement("button");
          btn.textContent = label;
          btn.style.cssText = "display:block;width:100%;text-align:left;padding:6px 12px;color:#ded8ce;background:transparent;border:none;border-radius:4px;font-size:13px;cursor:pointer";
          btn.onmouseenter = () => { btn.style.background = "#363028"; };
          btn.onmouseleave = () => { btn.style.background = "transparent"; };
          btn.onclick = () => { action(); menu.remove(); };
          menu.appendChild(btn);
        });
        document.body.appendChild(menu);
        const remove = () => { menu.remove(); document.removeEventListener("click", remove); };
        setTimeout(() => document.addEventListener("click", remove), 0);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") onClick();
      }}
      style={{
        position: "fixed",
        bottom: (16 + dragOffset.y) + "px",
        right: (16 + dragOffset.x) + "px",
        width: "44px",
        height: "44px",
        cursor: "grab",
        zIndex: 2147483647,
      }}
      className="rounded-full bg-neutral-900/80 backdrop-blur-md border border-white/8
        flex items-center justify-center
        transition-transform hover:scale-105 active:scale-95
        select-none motion-safe:animate-pulse-slow
        shadow-[0_0_16px_rgba(232,183,48,0.15),0_2px_8px_rgba(0,0,0,0.2)]
        focus-visible:ring-2 focus-visible:ring-brand-400/50 focus-visible:outline-none"
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLElement).style.cursor = "grab";
      }}
    >
      <svg
        width="20"
        height="20"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="text-brand-400"
      >
        <circle cx="12" cy="12" r="10" />
        <path d="M8 12a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1v-4Z" />
        <path d="M14 8a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-2a1 1 0 0 1-1-1V8Z" />
      </svg>
    </div>
  );
};