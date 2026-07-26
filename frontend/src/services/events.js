export function connectRunEvents(runId, { onEvent, onState }) {
  let source;
  let stopped = false;
  let retry = 1000;
  let timer;

  const connect = () => {
    if (stopped) return;
    onState?.("connecting");
    source = new EventSource(`/api/checks/${encodeURIComponent(runId)}/events`, { withCredentials: true });
    source.onopen = () => {
      retry = 1000;
      onState?.("connected");
    };
    source.onmessage = ({ data, lastEventId }) => {
      try { onEvent?.({ ...JSON.parse(data), event_id: lastEventId }); } catch { /* keep stream alive */ }
    };
    [
      "run.started", "scan.progress", "order.detected", "order.checked",
      "order.waiting_confirmation", "pdf.created", "preview.created",
      "order.completed", "order.correction_confirmed",
      "order.correction_rejected", "run.cancelling", "run.cancelled",
      "run.completed", "run.failed",
    ].forEach((type) => source.addEventListener(type, (event) => {
      try { onEvent?.({ ...JSON.parse(event.data), type, event_id: event.lastEventId }); } catch { /* ignore malformed event */ }
    }));
    source.onerror = () => {
      source.close();
      onState?.("reconnecting");
      timer = window.setTimeout(connect, retry);
      retry = Math.min(retry * 2, 15000);
    };
  };

  connect();
  return () => {
    stopped = true;
    window.clearTimeout(timer);
    source?.close();
    onState?.("closed");
  };
}
