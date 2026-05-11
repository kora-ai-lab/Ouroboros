import React, { useState, useEffect, useCallback, type FC } from "react";
import { getHardwareInfo, completeOnboarding } from "../../lib/tauri";
import type { HardwareInfo } from "../../lib/tauri";
import { WelcomeStep } from "./welcome-step";
import { HardwareStep } from "./hardware-step";
import { ReadyStep } from "./ready-step";

type Step = 1 | 2 | 3;

export const OnboardingPage: FC = () => {
  const [step, setStep] = useState<Step>(1);
  const [hardware, setHardware] = useState<HardwareInfo | null>(null);

  useEffect(() => {
    getHardwareInfo()
      .then((h) => setHardware(h))
      .catch(() => setHardware(null));
  }, []);

  const handleNext = useCallback(() => {
    setStep((s) => Math.min(s + 1, 3) as Step);
  }, []);

  const handleDismiss = useCallback(() => {
    completeOnboarding().catch(console.error);
  }, []);

  return React.createElement(
    "div",
    {
      className:
        "w-full h-screen bg-neutral-950 flex flex-col overflow-hidden select-none",
    },
    step === 1 && React.createElement(WelcomeStep, { onNext: handleNext }),
    step === 2 &&
      React.createElement(HardwareStep, {
        hardware: hardware,
        onNext: handleNext,
      }),
    step === 3 && React.createElement(ReadyStep, { onDismiss: handleDismiss })
  );
};