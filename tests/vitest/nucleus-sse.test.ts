import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const html = readFileSync("nucleus/index.html", "utf8");

const taskEvents = [
  "task_started",
  "task_plan",
  "task_step",
  "task_observation",
  "task_evaluation",
  "task_retry",
  "task_checkpoint",
  "task_done",
];

describe("Nucleus SSE task event rendering", () => {
  it("registers labels for all task protocol events", () => {
    for (const eventName of taskEvents) {
      expect(html).toContain(`${eventName}:`);
    }
  });

  it("routes task events without changing tool event handlers", () => {
    expect(html).toContain("TASK_EVENT_LABELS[eventName]");
    expect(html).toContain("renderTaskEvent(eventName, data)");
    expect(html).toContain("eventName === 'tool_call'");
    expect(html).toContain("eventName === 'tool_result'");
  });
});
