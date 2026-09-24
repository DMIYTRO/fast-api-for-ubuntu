const ORDER_EVENTS_WITH_FILES = new Set([
  "order.detected",
  "order.waiting_confirmation",
  "order.completed",
]);

const RUN_FINAL_EVENTS = new Set([
  "scan.progress",
  "run.completed",
  "run.failed",
  "run.cancelled",
]);

export function createRunFileProgress(runId = null) {
  return {
    runId: runId == null ? null : String(runId),
    fileCountsByOrder: {},
    processedOrders: {},
    finalized: false,
  };
}

export function applyRunFileProgressEvent(progress, event) {
  if (!progress || !event) return progress;
  if (event.run_id != null && progress.runId != null && String(event.run_id) !== progress.runId) {
    return progress;
  }

  if (ORDER_EVENTS_WITH_FILES.has(event.type) && event.order) {
    const orderId = String(event.order.aggregate_id ?? event.order.order_id ?? event.order.id ?? "");
    const files = event.order.files || event.order.file_results;
    if (orderId && Array.isArray(files)) {
      progress.fileCountsByOrder[orderId] = files.length;
      // order.completed is emitted for successful and failed terminal outcomes.
      if (event.type === "order.completed") progress.processedOrders[orderId] = true;
    }
  }

  if (RUN_FINAL_EVENTS.has(event.type)) progress.finalized = true;
  return progress;
}

export function summarizeRunFileProgress(progress) {
  const counts = Object.values(progress?.fileCountsByOrder || {});
  const totalFiles = counts.reduce((sum, count) => sum + count, 0);
  const processedFiles = Object.entries(progress?.processedOrders || {}).reduce(
    (sum, [orderId, processed]) => sum + (processed ? Number(progress.fileCountsByOrder[orderId] || 0) : 0),
    0,
  );
  return {
    discoveredFiles: totalFiles,
    totalFiles: progress?.finalized ? totalFiles : null,
    processedFiles,
    remainingFiles: progress?.finalized ? Math.max(0, totalFiles - processedFiles) : null,
    finalized: Boolean(progress?.finalized),
  };
}
