export function connectRunEvents(runId, { onEvent, onState }) {
  let source;
  let stopped = false;
  let retry = 1000;
  let timer;
  // Highest event id received so far. EventSource.close() discards the
  // browser's own Last-Event-ID, and a header cannot be set on EventSource, so a
  // reconnect passes it as ?last_event_id= and the server sends only newer events.
  // The first connect has no cursor and replays the run's history: the store
  // builds its file progress and event log from events only.
  let lastEventId = null;

  const remember = (rawId) => {
    const id = Number.parseInt(rawId, 10);
    if (Number.isFinite(id) && id >= 0 && (lastEventId === null || id > lastEventId)) lastEventId = id;
  };

  const streamUrl = () => {
    const base = `/api/checks/${encodeURIComponent(runId)}/events`;
    return lastEventId === null ? base : `${base}?last_event_id=${lastEventId}`;
  };

  const connect = () => {
    if (stopped) return;
    onState?.("connecting");
    source = new EventSource(streamUrl(), { withCredentials: true });
    source.onopen = () => {
      retry = 1000;
      onState?.("connected");
    };
    source.onmessage = ({ data, lastEventId: eventId }) => {
      remember(eventId);
      try { onEvent?.({ ...JSON.parse(data), event_id: eventId }); } catch { /* keep stream alive */ }
    };
    [
      "run.started", "scan.progress", "order.detected", "order.checked",
      "order.waiting_confirmation", "pdf.created", "preview.created",
      "order.completed", "order.pdf_revision", "order.correction_confirmed",
      "order.correction_rejected", "run.cancelling", "run.cancelled",
      "run.completed", "run.failed",
    ].forEach((type) => source.addEventListener(type, (event) => {
      remember(event.lastEventId);
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
