import React, { type FC } from "react";

interface ReadyStepProps {
  onDismiss: () => void;
}

export const ReadyStep: FC<ReadyStepProps> = ({ onDismiss }) => {
  return React.createElement(
    "div",
    {
      onClick: onDismiss,
      className:
        "flex flex-col items-center justify-center h-full gap-6 px-8 cursor-pointer animate-fade-in",
    },
    React.createElement(
      "div",
      { className: "w-16 h-16 rounded-full bg-amber-500/20 flex items-center justify-center animate-pulse-slow" },
      React.createElement("div", {
        className: "w-10 h-10 rounded-full bg-amber-500",
      })
    ),
    React.createElement(
      "h2",
      { className: "font-display text-xl text-neutral-50" },
      "You\u2019re all set."
    ),
    React.createElement(
      "p",
      { className: "text-body-lg text-neutral-400" },
      "Click the bubble to start."
    )
  );
};