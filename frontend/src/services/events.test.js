import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { connectRunEvents } from "./events.js";

class FakeEventSource {
  static instances = [];
  constructor(url, options) {
    this.url = url;
    this.options = options;
    this.listeners = {};
    this.closed = false;
    FakeEventSource.instances.push(this);
  }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  close() { this.closed = true; }
  emit(type, payload, id) {
    this.listeners[type]({ data: JSON.stringify(payload), lastEventId: String(id) });
  }
}

describe("connectRunEvents", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("starts the first connection without a cursor so the run history is replayed", () => {
    connectRunEvents("run 1", { onEvent: vi.fn() });
    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe("/api/checks/run%201/events");
    expect(FakeEventSource.instances[0].options).toEqual({ withCredentials: true });
  });

  it("reconnects with the id of the last received event", () => {
    const onEvent = vi.fn();
    const onState = vi.fn();
    connectRunEvents("run-1", { onEvent, onState });
    const first = FakeEventSource.instances[0];
    first.emit("order.checked", { order_id: "1" }, 41);
    first.emit("order.completed", { order_id: "1" }, 42);
    first.onerror();
    expect(first.closed).toBe(true);
    expect(onState).toHaveBeenLastCalledWith("reconnecting");
    vi.advanceTimersByTime(1000);
    expect(FakeEventSource.instances).toHaveLength(2);
    expect(FakeEventSource.instances[1].url).toBe("/api/checks/run-1/events?last_event_id=42");
    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({ order_id: "1", type: "order.completed", event_id: "42" }));
  });

  it("delivers the late customer comment update to the store", () => {
    const onEvent = vi.fn();
    connectRunEvents("run-1", { onEvent });
    FakeEventSource.instances[0].emit(
      "order.customer_comment",
      { order_id: "1", order: { order_id: "1", customer_comment: "текст" } },
      9,
    );
    expect(onEvent).toHaveBeenCalledWith(expect.objectContaining({
      type: "order.customer_comment", event_id: "9", order: { order_id: "1", customer_comment: "текст" },
    }));
  });

  it("tracks ids of default (unnamed) messages and never moves the cursor backwards", () => {
    connectRunEvents("run-1", { onEvent: vi.fn() });
    const first = FakeEventSource.instances[0];
    first.onmessage({ data: JSON.stringify({ a: 1 }), lastEventId: "10" });
    first.emit("scan.progress", {}, 7);
    first.onmessage({ data: "not json", lastEventId: "" });
    first.onerror();
    vi.advanceTimersByTime(1000);
    expect(FakeEventSource.instances[1].url).toContain("last_event_id=10");
  });

  it("keeps the cursor across several reconnects and backs off", () => {
    connectRunEvents("run-1", { onEvent: vi.fn() });
    FakeEventSource.instances[0].emit("order.checked", {}, 5);
    FakeEventSource.instances[0].onerror();
    vi.advanceTimersByTime(1000);
    FakeEventSource.instances[1].onerror(); // no new events on this one
    vi.advanceTimersByTime(1999);
    expect(FakeEventSource.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeEventSource.instances).toHaveLength(3);
    expect(FakeEventSource.instances[2].url).toBe("/api/checks/run-1/events?last_event_id=5");
  });

  it("does not reconnect after the caller closes the stream", () => {
    const stop = connectRunEvents("run-1", { onEvent: vi.fn() });
    stop();
    FakeEventSource.instances[0].onerror?.();
    vi.advanceTimersByTime(20000);
    expect(FakeEventSource.instances[0].closed).toBe(true);
  });
});
