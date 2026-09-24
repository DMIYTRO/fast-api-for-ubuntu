import { describe, expect, it } from "vitest";
import { applyRunFileProgressEvent, createRunFileProgress, summarizeRunFileProgress } from "./runFileProgress.js";

describe("run file progress", () => {
  it("keeps the changing discovery count provisional until the scan ends", () => {
    const progress = createRunFileProgress("run-1");
    applyRunFileProgressEvent(progress, {
      run_id: "run-1", type: "order.detected",
      order: { order_id: "A", files: [{}, {}] },
    });
    expect(summarizeRunFileProgress(progress)).toEqual({
      discoveredFiles: 2, totalFiles: null, processedFiles: 0, remainingFiles: null, finalized: false,
    });

    applyRunFileProgressEvent(progress, {
      run_id: "run-1", type: "order.detected",
      order: { order_id: "B", files: [{}, {} , {}] },
    });
    expect(summarizeRunFileProgress(progress).totalFiles).toBeNull();
    expect(summarizeRunFileProgress(progress).discoveredFiles).toBe(5);

    applyRunFileProgressEvent(progress, { run_id: "run-1", type: "scan.progress" });
    expect(summarizeRunFileProgress(progress)).toEqual({
      discoveredFiles: 5, totalFiles: 5, processedFiles: 0, remainingFiles: 5, finalized: true,
    });
  });

  it("counts every file in terminal error orders as processed", () => {
    const progress = createRunFileProgress("run-1");
    applyRunFileProgressEvent(progress, {
      run_id: "run-1", type: "order.detected", order: { order_id: "bad", files: [{}, {}] },
    });
    applyRunFileProgressEvent(progress, {
      run_id: "run-1", type: "order.completed", status: "error",
      order: { order_id: "bad", status: "error", files: [{}, {}] },
    });
    applyRunFileProgressEvent(progress, { run_id: "run-1", type: "scan.progress" });
    expect(summarizeRunFileProgress(progress)).toMatchObject({
      totalFiles: 2, processedFiles: 2, remainingFiles: 0, finalized: true,
    });
  });

  it("does not mark an order awaiting confirmation as processed", () => {
    const progress = createRunFileProgress("run-1");
    applyRunFileProgressEvent(progress, {
      run_id: "run-1", type: "order.waiting_confirmation",
      order: { order_id: "pending", files: [{}, {}] },
    });
    applyRunFileProgressEvent(progress, { run_id: "run-1", type: "scan.progress" });
    expect(summarizeRunFileProgress(progress)).toMatchObject({
      totalFiles: 2, processedFiles: 0, remainingFiles: 2,
    });
  });

  it("keeps cancelled, unfinished files in the remaining count", () => {
    const progress = createRunFileProgress("run-1");
    applyRunFileProgressEvent(progress, {
      run_id: "run-1", type: "order.detected", order: { order_id: "done", files: [{}] },
    });
    applyRunFileProgressEvent(progress, {
      run_id: "run-1", type: "order.completed", order: { order_id: "done", files: [{}] },
    });
    applyRunFileProgressEvent(progress, {
      run_id: "run-1", type: "order.detected", order: { order_id: "stopped", files: [{}, {}] },
    });
    applyRunFileProgressEvent(progress, { run_id: "run-1", type: "run.cancelled" });
    expect(summarizeRunFileProgress(progress)).toMatchObject({
      totalFiles: 3, processedFiles: 1, remainingFiles: 2, finalized: true,
    });
  });

  it("ignores events from a different run", () => {
    const progress = createRunFileProgress("run-1");
    applyRunFileProgressEvent(progress, {
      run_id: "run-2", type: "order.detected", order: { order_id: "other", files: [{}] },
    });
    expect(summarizeRunFileProgress(progress).discoveredFiles).toBe(0);
  });
});
