import React, { type FC } from "react";

interface WelcomeStepProps {
  onNext: () => void;
}

export const WelcomeStep: FC<WelcomeStepProps> = ({ onNext }) => {
  return React.createElement(
    "div",
    {
      className:
        "flex flex-col items-center justify-center h-full gap-6 px-8 animate-fade-in",
    },
    React.createElement(
      "div",
      { className: "w-16 h-16 rounded-full bg-amber-500/20 flex items-center justify-center animate-pulse-slow" },
      React.createElement("div", {
        className: "w-10 h-10 rounded-full bg-amber-500",
      })
    ),
    React.createElement(
      "h1",
      { className: "font-display text-xl text-neutral-50 text-center" },
      "Your assistant is ready."
    ),
    React.createElement(
      "p",
      { className: "text-body-lg text-neutral-400 text-center max-w-sm" },
      "Click the bubble to ask questions, run tasks, or get things done. No tabs, no terminals."
    ),
    React.createElement(
      "button",
      {
        onClick: onNext,
        className:
          "px-6 py-3 bg-amber-500 text-neutral-900 rounded-lg font-medium hover:bg-amber-400 transition-colors focus-visible:ring-2 focus-visible:ring-amber-400/50 focus-visible:outline-none",
      },
      "Get started"
    )
  );
};