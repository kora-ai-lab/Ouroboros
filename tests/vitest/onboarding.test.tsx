import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import React from "react";

vi.mock("../../src/lib/tauri", () => ({
  getHardwareInfo: vi.fn(),
  completeOnboarding: vi.fn(),
}));

import { getHardwareInfo, completeOnboarding } from "../../src/lib/tauri";
import { OnboardingPage } from "../../src/components/onboarding/onboarding-page";

describe("OnboardingPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders welcome step initially", async () => {
    vi.mocked(getHardwareInfo).mockResolvedValue({
      os: "Windows",
      cpu_cores: 8,
      total_ram_mb: 16000,
      gpu_name: "NVIDIA RTX 3060",
      gpu_vram_mb: 12288,
      recommended_model: "Qwen 2.5",
    });

    render(React.createElement(OnboardingPage, null));

    await waitFor(() => {
      expect(screen.getByText("Your assistant is ready.")).toBeDefined();
    });
  });

  it("advances from welcome to hardware step", async () => {
    vi.mocked(getHardwareInfo).mockResolvedValue({
      os: "Windows",
      cpu_cores: 8,
      total_ram_mb: 16000,
      gpu_name: "NVIDIA RTX 3060",
      gpu_vram_mb: 12288,
      recommended_model: "Qwen 2.5",
    });

    render(React.createElement(OnboardingPage, null));
    await waitFor(() => screen.getByText("Your assistant is ready."));

    screen.getByText("Get started").click();

    await waitFor(() => {
      expect(screen.getByText(/NVIDIA RTX 3060/)).toBeDefined();
    });
  });

  it("reaches ready step and calls completeOnboarding", async () => {
    vi.mocked(getHardwareInfo).mockResolvedValue({
      os: "Windows",
      cpu_cores: 8,
      total_ram_mb: 16000,
      gpu_name: "NVIDIA RTX 3060",
      gpu_vram_mb: 12288,
      recommended_model: "Qwen 2.5",
    });
    vi.mocked(completeOnboarding).mockResolvedValue(undefined);

    render(React.createElement(OnboardingPage, null));
    await waitFor(() => screen.getByText("Your assistant is ready."));
    screen.getByText("Get started").click();

    await waitFor(() => screen.getByText(/NVIDIA RTX 3060/));
    screen.getByText("Skip \u2014 use cloud models").click();

    await waitFor(() => {
      expect(screen.getByText("You\u2019re all set.")).toBeDefined();
    });

    screen.getByText("Click the bubble to start.").click();

    await waitFor(() => {
      expect(completeOnboarding).toHaveBeenCalled();
    });
  });
});