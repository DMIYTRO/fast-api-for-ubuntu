import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApp, nextTick } from "vue";
import CallasFixControl from "./CallasFixControl.vue";
import { api, ApiError } from "../services/api.js";

const mounted = [];
function mount(props = {}) {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(CallasFixControl, { runId: "run-1", orderId: "agg-1", ...props });
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return { host, app };
}
const flush = async () => { for (let i = 0; i < 6; i += 1) { await Promise.resolve(); await nextTick(); } };
const byText = (host, text) => [...host.querySelectorAll("button")].find((b) => b.textContent.includes(text));
const plan = { key: "size", title: "Коррекция размера" };
const proc = { comparison: { status: "processing", plan } };
const ready = { comparison: { status: "ready", plan, original_url: "/o", result_url: "/r", overlay_url: "/v", summary: {} } };

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, status: 200, blob: async () => new Blob(["x"]) })));
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: vi.fn() });
});
afterEach(() => {
  mounted.splice(0).forEach((unmount) => unmount());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("api methods", () => {
  it("call the expected endpoints", async () => {
    const fetchMock = vi.fn(() => Promise.resolve({ ok: true, status: 200, headers: new Headers({ "content-type": "application/json" }), json: async () => ({}) }));
    vi.stubGlobal("fetch", fetchMock);
    await api.callasPlans();
    await api.startCallasFix("r 1", "a b", "size");
    await api.callasFixStatus("r 1", "a b");
    await api.decideCallasFix("r 1", "a b", "accept");
    const calls = fetchMock.mock.calls.map(([url, init]) => [url, init.method || "GET", init.body]);
    expect(calls).toEqual([
      ["/api/callas/plans", "GET", undefined],
      ["/api/checks/r%201/orders/a%20b/callas/fix", "POST", JSON.stringify({ plan: "size" })],
      ["/api/checks/r%201/orders/a%20b/callas/fix", "GET", undefined],
      ["/api/checks/r%201/orders/a%20b/callas/fix/decision", "POST", JSON.stringify({ decision: "accept" })],
    ]);
  });
});

describe("CallasFixControl", () => {
  it("loads plans on demand, starts the fix and polls until ready, then stops", async () => {
    vi.spyOn(api, "callasPlans").mockResolvedValue({ plans: [plan] });
    const start = vi.spyOn(api, "startCallasFix").mockResolvedValue(proc);
    const status = vi.spyOn(api, "callasFixStatus")
      .mockResolvedValueOnce(proc)
      .mockResolvedValue(ready);
    const { host } = mount();
    await nextTick();
    expect(api.callasPlans).not.toHaveBeenCalled();
    byText(host, "Исправить").click();
    await flush();
    expect(host.querySelector("select option").textContent).toBe("Коррекция размера");
    byText(host, "Запустить").click();
    await flush();
    expect(start).toHaveBeenCalledWith("run-1", "agg-1", "size");
    expect(host.querySelector(".fix-processing")).not.toBeNull();

    await vi.advanceTimersByTimeAsync(1600);
    await flush();
    expect(status).toHaveBeenCalledTimes(1);
    expect(host.querySelector(".fix-processing")).not.toBeNull();
    await vi.advanceTimersByTimeAsync(1600);
    await flush();
    expect(status).toHaveBeenCalledTimes(2);
    expect(host.querySelector(".fix-ready")).not.toBeNull();

    await vi.advanceTimersByTimeAsync(10000);
    expect(status).toHaveBeenCalledTimes(2);
  });

  it("stops polling when unmounted", async () => {
    const status = vi.spyOn(api, "callasFixStatus").mockResolvedValue(proc);
    const { app } = mount({ initial: proc });
    await nextTick();
    await vi.advanceTimersByTimeAsync(1600);
    expect(status).toHaveBeenCalledTimes(1);
    app.unmount();
    await vi.advanceTimersByTimeAsync(10000);
    expect(status).toHaveBeenCalledTimes(1);
  });

  it("sends the decision and shows the outcome", async () => {
    const decide = vi.spyOn(api, "decideCallasFix").mockResolvedValue({ comparison: { ...ready.comparison, status: "accepted" } });
    const { host } = mount({ initial: ready });
    await nextTick();
    byText(host, "Принять").click();
    await flush();
    expect(decide).toHaveBeenCalledWith("run-1", "agg-1", "accept");
    expect(host.querySelector(".fix-accepted")).not.toBeNull();
    expect(host.querySelector(".fix-actions")).toBeNull();
  });

  it("retries a failed fix with the same plan", async () => {
    const start = vi.spyOn(api, "startCallasFix").mockResolvedValue(proc);
    vi.spyOn(api, "callasFixStatus").mockResolvedValue(proc);
    const { host } = mount({ initial: { comparison: { status: "failed", plan, error: "boom" } } });
    await nextTick();
    byText(host, "Повторить").click();
    await flush();
    expect(start).toHaveBeenCalledWith("run-1", "agg-1", "size");
    expect(host.querySelector(".fix-processing")).not.toBeNull();
  });

  it("shows a compact notice and keeps the card working when the endpoint is missing (404)", async () => {
    vi.spyOn(api, "callasPlans").mockRejectedValue(new ApiError("Not Found", 404, {}));
    const { host } = mount();
    await nextTick();
    byText(host, "Исправить").click();
    await flush();
    expect(host.textContent).toContain("Исправление Callas недоступно");
    expect(byText(host, "Исправить")).toBeUndefined();
  });

  it("renders nothing for an order that cannot start and has no result", async () => {
    const { host } = mount({ canStart: false });
    await nextTick();
    expect(host.querySelector(".callas-fix")).toBeNull();
  });
});
