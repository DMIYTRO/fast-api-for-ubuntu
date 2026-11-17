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

  it("explains an empty overlay for structural fixes and always shows the legend", async () => {
    setup();
    const structural = mount({ comparison: { ...base, status: "ready", summary: { changed_percent: 0, overlay_changed_percent: 0 } } });
    await nextTick();
    expect(structural.textContent).toContain("Внешний вид макета не изменился");
    expect(structural.querySelector(".fix-legend")).not.toBeNull();

    const visible = mount({ comparison: { ...base, status: "ready", summary: { changed_percent: 0, overlay_changed_percent: 1.5 } } });
    await nextTick();
    expect(visible.textContent).not.toContain("Внешний вид макета не изменился");
  });

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

  it("notes non-uniform scaling with both axes and does not rely on colour only", async () => {
    setup();
    const host = mount({ comparison: { ...base, status: "ready", summary: { ...base.summary, distortion: true } } });
    await nextTick();
    const warning = host.querySelector(".fix-distortion");
    expect(warning.getAttribute("role")).toBe("status");
    expect(warning.textContent).toContain("Масштаб по осям");
    expect(warning.textContent).toContain("Макет растянут неравномерно (по ширине ×0,990, по высоте ×1,049)");
    expect(warning.textContent).toContain("Проверьте наложение перед принятием");
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

  it("offers accept and reject for a source PDF fix only when the backend allows it", async () => {
    setup();
    const onAccept = vi.fn();
    const onReject = vi.fn();
    const acceptable = mount({ comparison: { ...base, status: "ready", source_only: true, can_accept: true, pdf_url: "/p.pdf" } }, { onAccept, onReject });
    await nextTick();
    byText(acceptable, "Принять").click();
    byText(acceptable, "Отклонить").click();
    expect(onAccept).toHaveBeenCalledTimes(1);
    expect(onReject).toHaveBeenCalledTimes(1);
    expect(acceptable.querySelector("a[href='/p.pdf']")).toBeNull();
    expect(acceptable.textContent).not.toContain("принять исправление в печать отсюда нельзя");
    expect(acceptable.textContent).toContain("этот файл будет собран из исправленного PDF");

    const previewOnly = mount({ comparison: { ...base, status: "ready", source_only: true, can_accept: false, pdf_url: "/p.pdf" } });
    await nextTick();
    expect(byText(previewOnly, "Принять")).toBeUndefined();
    expect(byText(previewOnly, "Отклонить")).toBeUndefined();
    expect(previewOnly.querySelector("a[href='/p.pdf']")).not.toBeNull();
    expect(previewOnly.textContent).toContain("принять исправление в печать отсюда нельзя");
  });

  it.each([
    ["accepted", "Принято: этот файл будет собран из исправленного PDF"],
    ["rejected", "Исправление отклонено: файл проверяется по исходному файлу"],
  ])("shows the decided %s state of a source PDF fix", async (status, text) => {
    setup();
    const host = mount({ comparison: { ...base, status, source_only: true, applied: status === "accepted", can_accept: false, pdf_url: "/p.pdf" } });
    await nextTick();
    expect(host.textContent).toContain(text);
    expect(host.querySelector(".fix-actions")).toBeNull();
    expect(host.querySelector("a[href='/p.pdf']")).toBeNull();
  });

  describe("bleed-only note for the size correction plan", () => {
    const sizePlan = { key: "size_correction", title: "Коррекция размера" };
    const withSummary = (extra, plan = sizePlan) => ({ ...base, plan, status: "ready", summary: { ...base.summary, ...extra } });

    it("shows a status note when only the bleed changed", async () => {
      setup();
      const host = mount({ comparison: withSummary({ bleed_only: true, bleed_only_reasons: [] }) });
      await nextTick();
      const note = host.querySelector(".fix-bleed-only");
      expect(note.getAttribute("role")).toBe("status");
      expect(note.textContent).toContain("Только поля: макет внутри обрезного формата не изменён");
      expect(host.querySelector(".fix-bleed-changed")).toBeNull();
    });

    it("warns and lists the reasons when the layout changed beyond the bleed", async () => {
      setup();
      const host = mount({ comparison: withSummary({ bleed_only: false, bleed_only_reasons: ["масштаб макета изменён", "обрезной формат сдвинут"] }) });
      await nextTick();
      const note = host.querySelector(".fix-bleed-changed");
      expect(note.getAttribute("role")).toBe("status");
      expect(note.classList.contains("fix-plan-warning")).toBe(true);
      expect(note.textContent).toContain("Фикс изменил макет, не только поля:");
      expect([...note.querySelectorAll("li")].map((li) => li.textContent)).toEqual(["масштаб макета изменён", "обрезной формат сдвинут"]);
      expect(host.querySelector(".fix-bleed-only")).toBeNull();
    });

    it("shows nothing for old payloads without the field or for other plans", async () => {
      setup();
      const absent = mount({ comparison: withSummary({}) });
      await nextTick();
      expect(absent.querySelector(".fix-bleed-only")).toBeNull();
      expect(absent.querySelector(".fix-bleed-changed")).toBeNull();
      expect(absent.textContent).not.toContain("Только поля");

      const other = mount({ comparison: withSummary({ bleed_only: true }, { key: "fonts_to_outlines", title: "Шрифты в кривые" }) });
      await nextTick();
      expect(other.querySelector(".fix-bleed-only")).toBeNull();
      expect(other.querySelector(".fix-bleed-changed")).toBeNull();
    });
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
