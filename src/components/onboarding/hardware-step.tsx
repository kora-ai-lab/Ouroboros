import React, { useState, useEffect, useRef, useCallback, type FC } from "react";
import type { HardwareInfo } from "../../lib/tauri";

type StepState = "loading" | "result" | "downloading" | "done" | "error";

interface HardwareStepProps {
  hardware: HardwareInfo | null;
  onNext: () => void;
}

const downloadProgressBar = (pct: number) =>
  React.createElement(
    "div",
    { className: "w-full bg-neutral-800 rounded-full h-2 overflow-hidden" },
    React.createElement("div", {
      className: "h-full bg-amber-500 rounded-full transition-all duration-300",
      style: { width: `${pct}%` },
    })
  );

function hwDisplayLine(info: HardwareInfo): string {
  const gpu = info.gpu_name || "CPU";
  const vram = info.gpu_vram_mb ? `with ${Math.round(info.gpu_vram_mb / 1024)}GB` : "";
  const model = info.recommended_model || "local models";
  return `Your ${gpu} ${vram} can run ${model}. Fast, private, and offline.`;
}

export const HardwareStep: FC<HardwareStepProps> = ({ hardware, onNext }) => {
  const [stepState, setStepState] = useState<StepState>("loading");
  const [downloadProgress, setDownloadProgress] = useState(0);
  const cancelRef = useRef(false);
  const intervalRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined);

  useEffect(() => {
    if (hardware || stepState !== "loading") return;
    const t = setTimeout(() => setStepState("result"), 2500);
    return () => clearTimeout(t);
  }, [hardware, stepState]);

  useEffect(() => {
    if (hardware && stepState === "loading") {
      setStepState("result");
    }
  }, [hardware, stepState]);

  const handleDownload = useCallback(() => {
    setStepState("downloading");
    setDownloadProgress(0);
    cancelRef.current = false;

    let pct = 0;
    intervalRef.current = setInterval(() => {
      if (cancelRef.current) {
        clearInterval(intervalRef.current);
        setStepState("result");
        return;
      }
      pct += 10;
      if (pct >= 100) {
        clearInterval(intervalRef.current);
        setDownloadProgress(100);
        setStepState("done");
        setTimeout(onNext, 1000);
      } else {
        setDownloadProgress(pct);
      }
    }, 300);
  }, [onNext]);

  const handleCancel = useCallback(() => {
    cancelRef.current = true;
  }, []);

  const handleSkip = useCallback(() => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    onNext();
  }, [onNext]);

  return React.createElement(
    "div",
    {
      className:
        "flex flex-col items-center justify-center h-full gap-5 px-8 animate-fade-in",
    },
    stepState === "loading" &&
      React.createElement(
        "div",
        { className: "flex flex-col items-center gap-5" },
        React.createElement(
          "div",
          { className: "w-12 h-12 border-4 border-amber-500/30 border-t-amber-500 rounded-full animate-spin" }
        ),
        React.createElement(
          "h2",
          { className: "font-display text-lg text-neutral-50" },
          "Checking what your computer can run"
        ),
        React.createElement(
          "p",
          { className: "text-body text-neutral-500" },
          "This takes a few seconds."
        )
      ),
    stepState === "result" &&
      React.createElement(
        "div",
        { className: "flex flex-col items-center gap-5" },
        hardware
          ? React.createElement(
              "div",
              { className: "flex flex-col items-center gap-3" },
              React.createElement(
                "div",
                { className: "w-12 h-12 rounded-full bg-emerald-500/20 flex items-center justify-center" },
                React.createElement("span", { className: "text-emerald-400 text-xl" }, "\u2713")
              ),
              React.createElement(
                "h2",
                { className: "font-display text-lg text-neutral-50 text-center" },
                hwDisplayLine(hardware)
              )
            )
          : React.createElement(
              "div",
              { className: "flex flex-col items-center gap-3" },
              React.createElement(
                "div",
                { className: "w-12 h-12 rounded-full bg-amber-500/20 flex items-center justify-center" },
                React.createElement("span", { className: "text-amber-400 text-xl" }, "\u0021")
              ),
              React.createElement(
                "h2",
                { className: "font-display text-lg text-neutral-50 text-center" },
                "Your computer may struggle with local models. You can still use Ouroboros with cloud providers."
              )
            ),
        React.createElement(
          "div",
          { className: "flex flex-col gap-3 w-full max-w-xs" },
          hardware &&
            React.createElement(
              "button",
              {
                onClick: handleDownload,
                className:
                  "px-6 py-3 bg-amber-500 text-neutral-900 rounded-lg font-medium hover:bg-amber-400 transition-colors focus-visible:ring-2 focus-visible:ring-amber-400/50 focus-visible:outline-none",
              },
              `Download ${hardware.recommended_model || "model"} (${hardware.gpu_vram_mb ? Math.round(hardware.gpu_vram_mb / 1024 * 1.5) : 4}GB)`
            ),
          React.createElement(
            "button",
            {
              onClick: handleSkip,
              className:
                "px-6 py-3 bg-transparent border border-neutral-700 text-neutral-300 rounded-lg font-medium hover:border-neutral-500 transition-colors focus-visible:ring-2 focus-visible:ring-amber-400/50 focus-visible:outline-none",
            },
            "Skip \u2014 use cloud models"
          )
        )
      ),
    stepState === "downloading" &&
      React.createElement(
        "div",
        { className: "flex flex-col items-center gap-4 w-full max-w-xs" },
        React.createElement(
          "h2",
          { className: "font-display text-lg text-neutral-50" },
          "Downloading model..."
        ),
        downloadProgressBar(downloadProgress),
        React.createElement(
          "p",
          { className: "text-body-sm text-neutral-500" },
          `${downloadProgress}%`
        ),
        React.createElement(
          "button",
          {
            onClick: handleCancel,
            className:
              "px-4 py-2 bg-transparent border border-neutral-700 text-neutral-400 rounded-lg text-sm hover:border-neutral-500 transition-colors focus-visible:ring-2 focus-visible:ring-amber-400/50 focus-visible:outline-none",
          },
          "Cancel"
        )
      ),
    stepState === "done" &&
      React.createElement(
        "div",
        { className: "flex flex-col items-center gap-3" },
        React.createElement("span", { className: "text-emerald-400 text-2xl" }, "\u2713"),
        React.createElement(
          "p",
          { className: "text-body text-neutral-300" },
          "Ready to use"
        )
      ),
    stepState === "error" &&
      React.createElement(
        "div",
        { className: "flex flex-col items-center gap-4" },
        React.createElement(
          "h2",
          { className: "font-display text-lg text-neutral-50" },
          "Download didn\u2019t complete."
        ),
        React.createElement(
          "div",
          { className: "flex gap-3" },
          React.createElement(
            "button",
            {
              onClick: handleDownload,
              className:
                "px-4 py-2 bg-amber-500 text-neutral-900 rounded-lg text-sm font-medium hover:bg-amber-400 transition-colors",
            },
            "Try again"
          ),
          React.createElement(
            "button",
            {
              onClick: handleSkip,
              className:
                "px-4 py-2 bg-transparent border border-neutral-700 text-neutral-300 rounded-lg text-sm hover:border-neutral-500 transition-colors",
            },
            "Skip"
          )
        )
      )
  );
};