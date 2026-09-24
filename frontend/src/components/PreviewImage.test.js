import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApp, h, nextTick, ref } from "vue";
import PreviewImage from "./PreviewImage.vue";

const mounted = [];
let observers = [];
let calls = [];
let ids = 0;
class MockObserver {
  constructor(callback) { this.callback = callback; this.nodes = []; observers.push(this); }
  observe(node) { this.nodes.push(node); }
  disconnect() {}
  intersectAll() { this.callback(this.nodes.map((target) => ({ target, isIntersecting: true }))); }
  intersectNode(target) { this.callback([{ target, isIntersecting: true }]); }
}
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
const ok = () => ({ ok: true, status: 200, blob: async () => new Blob(["preview"]) });
function mount(src, eager = false) {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(PreviewImage, { src, eager });
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return { app, host };
}
function mountReactive(src) {
  const value = ref(src);
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp({ setup: () => () => h(PreviewImage, { src: value.value, eager: true }) });
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return { setSrc: (next) => { value.value = next; } };
}
async function flush() { await Promise.resolve(); await Promise.resolve(); await nextTick(); }

beforeEach(() => {
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => `blob:preview-${++ids}`), revokeObjectURL: vi.fn() });
  vi.stubGlobal("fetch", vi.fn((src, options) => {
    const wait = deferred();
    calls.push({ src, options, ...wait });
    return wait.promise;
  }));
});
afterEach(() => {
  mounted.splice(0).forEach((unmount) => unmount());
  observers = [];
  calls = [];
  ids = 0;
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("PreviewImage", () => {
  it("waits for the card to approach the viewport and uses one same-origin fetch", async () => {
    vi.stubGlobal("IntersectionObserver", MockObserver);
    mount("/api/files/file-1/preview?size=thumbnail");
    await nextTick();
    expect(fetch).not.toHaveBeenCalled();
    observers[0].intersectAll();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith("/api/files/file-1/preview?size=thumbnail", expect.objectContaining({ credentials: "same-origin", signal: expect.any(AbortSignal) }));
    calls[0].resolve(ok());
    await flush();
    expect(document.querySelector(".preview-loader img").getAttribute("src")).toBe("blob:preview-1");
  });

  it("limits in-flight fetches to four and starts the next card when a fetch finishes", async () => {
    for (let index = 0; index < 6; index += 1) mount(`/thumb/${index}`, true);
    await flush();
    expect(fetch).toHaveBeenCalledTimes(4);
    calls[0].resolve(ok());
    await flush();
    expect(fetch).toHaveBeenCalledTimes(5);
  });

  it("prioritizes a visible preview over queued near-viewport previews", async () => {
    vi.stubGlobal("IntersectionObserver", MockObserver);
    for (let index = 0; index < 4; index += 1) mount(`/thumb/active-${index}`, true);
    await flush();
    const queued = mount("/thumb/near", false);
    await nextTick();
    const nearObserver = observers.at(-2);
    nearObserver.intersectNode(queued.host.querySelector("img"));
    const visible = mount("/thumb/visible", false);
    await nextTick();
    observers.at(-1).intersectNode(visible.host.querySelector("img"));
    calls[0].resolve(ok());
    await flush();
    expect(calls.map((call) => call.src)).toContain("/thumb/visible");
    expect(calls.map((call) => call.src)).not.toContain("/thumb/near");
  });

  it("aborts a replaced URL and ignores its late response without freeing the new slot", async () => {
    const changing = mountReactive("/thumb/A");
    await flush();
    const oldCall = calls[0];
    changing.setSrc("/thumb/B");
    await flush();
    expect(oldCall.options.signal.aborted).toBe(true);
    ["C", "D", "E", "F"].forEach((name) => mount(`/thumb/${name}`, true));
    await flush();
    expect(fetch).toHaveBeenCalledTimes(5);
    oldCall.resolve(ok());
    await flush();
    expect(fetch).toHaveBeenCalledTimes(5);
    calls.find((call) => call.src === "/thumb/B").resolve(ok());
    await flush();
    expect(fetch).toHaveBeenCalledTimes(6);
    expect(calls[5].src).toBe("/thumb/F");
  });

  it("retries transient errors three times then offers a visible manual retry", async () => {
    vi.useFakeTimers();
    fetch.mockRejectedValue(new Error("temporary share error"));
    mount("/thumb/retry", true);
    await flush();
    await vi.advanceTimersByTimeAsync(1200);
    await flush();
    expect(fetch).toHaveBeenCalledTimes(3);
    const button = document.querySelector(".preview-load-retry");
    expect(button?.textContent).toContain("Повторить загрузку");
    fetch.mockImplementationOnce(async () => ok());
    button.click();
    await flush();
    expect(fetch).toHaveBeenCalledTimes(4);
  });
});
