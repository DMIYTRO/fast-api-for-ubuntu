import { afterEach, describe, expect, it, vi } from "vitest";
import { createApp, nextTick } from "vue";
import FixComparison from "./FixComparison.vue";

const mounted = [];
const ok = () => Promise.resolve({ ok: true, status: 200, blob: async () => new Blob(["img"]) });

function mount(props, handlers = {}) {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(FixComparison, { ...props, ...handlers });
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return host;
}
const base = {
  plan: { key: "size", title: "Коррекция размера" },
  original_url: "/o.png",
  result_url: "/r.png",
  overlay_url: "/v.png",
  summary: {
    original_size_mm: [104, 204],
    result_size_mm: [103, 214],
    changed_percent: 12.34,
    scale_x: 0.99,
    scale_y: 1.049,
    distortion: false,
  },
};
const buttons = (host) => [...host.querySelectorAll("button")].map((b) => b.textContent.trim());
const byText = (host, text) => [...host.querySelectorAll("button")].find((b) => b.textContent.includes(text));

afterEach(() => {
  mounted.splice(0).forEach((unmount) => unmount());
  vi.unstubAllGlobals();
});

describe("FixComparison", () => {
  const setup = () => {
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: vi.fn() });
    vi.stubGlobal("fetch", vi.fn(ok));
  };

  it("shows plan title, three tiles and change summary when ready", async () => {
    setup();
    const host = mount({ comparison: { ...base, status: "ready" } });
    await nextTick();
    expect(host.textContent).toContain("Коррекция размера");
    expect([...host.querySelectorAll(".fix-tile figcaption")].map((n) => n.textContent))
      .toEqual(["Оригинал", "Результат", "Наложение"]);
    expect(host.textContent).toContain("Размер 104 × 204 → 103 × 214 мм");
    expect(host.textContent).toContain("Изменено 12,3 % площади");
    expect(host.querySelector(".fix-distortion")).toBeNull();
  });

  it("warns about non-uniform scaling with both axes and does not rely on colour only", async () => {
    setup();
    const host = mount({ comparison: { ...base, status: "ready", summary: { ...base.summary, distortion: true } } });
    await nextTick();
    const warning = host.querySelector(".fix-distortion");
    expect(warning.getAttribute("role")).toBe("alert");
    expect(warning.textContent).toContain("Внимание");
    expect(warning.textContent).toContain("Макет масштабирован неравномерно (по ширине ×0,990, по высоте ×1,049)");
    expect(warning.textContent).toContain("нужно подтверждение");
    expect(byText(host, "Принять").getAttribute("aria-describedby")).toBe(warning.id);
  });

  it("emits accept and reject from the ready state", async () => {
    setup();
    const onAccept = vi.fn();
    const onReject = vi.fn();
    const host = mount({ comparison: { ...base, status: "ready" } }, { onAccept, onReject });
    await nextTick();
    byText(host, "Принять").click();
    byText(host, "Отклонить").click();
    expect(onAccept).toHaveBeenCalledTimes(1);
    expect(onReject).toHaveBeenCalledTimes(1);
  });

  it("disables decision buttons while busy", async () => {
    setup();
    const onAccept = vi.fn();
    const host = mount({ comparison: { ...base, status: "ready" }, busy: true }, { onAccept });
    await nextTick();
    expect(byText(host, "Принять").disabled).toBe(true);
    expect(byText(host, "Отклонить").disabled).toBe(true);
    byText(host, "Принять").click();
    expect(onAccept).not.toHaveBeenCalled();
  });

  it("shows an indicator and no buttons while processing", async () => {
    setup();
    const host = mount({ comparison: { status: "processing", plan: base.plan } });
    await nextTick();
    expect(host.querySelector("[role=status] .spinner")).not.toBeNull();
    expect(host.querySelector(".fix-tile")).toBeNull();
    expect(buttons(host)).toEqual([]);
  });

  it("shows the error and emits retry when failed", async () => {
    setup();
    const onRetry = vi.fn();
    const host = mount({ comparison: { status: "failed", plan: base.plan, error: "Callas недоступен" } }, { onRetry });
    await nextTick();
    expect(host.querySelector("[role=alert]").textContent).toContain("Callas недоступен");
    expect(buttons(host)).toEqual(["Повторить"]);
    byText(host, "Повторить").click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["accepted", "Исправление принято"],
    ["rejected", "Исправление отклонено"],
  ])("shows the outcome without buttons when %s", async (status, text) => {
    setup();
    const host = mount({ comparison: { ...base, status } });
    await nextTick();
    expect(host.textContent).toContain(text);
    expect(host.querySelector(".fix-actions")).toBeNull();
    expect(host.querySelectorAll(".fix-tile").length).toBe(3);
  });

  it("opens a keyboard-accessible lightbox for a tile and closes it with Escape", async () => {
    setup();
    const host = mount({ comparison: { ...base, status: "ready" } });
    await nextTick();
    const tile = host.querySelector(".fix-tile-overlay .preview-image");
    expect(tile.getAttribute("tabindex")).toBe("0");
    expect(tile.getAttribute("aria-label")).toContain("Наложение");
    tile.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    await nextTick();
    const dialog = document.querySelector(".lightbox");
    expect(dialog.getAttribute("role")).toBe("dialog");
    dialog.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await nextTick();
    expect(document.querySelector(".lightbox")).toBeNull();
  });
});
