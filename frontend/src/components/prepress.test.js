import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApp, nextTick } from "vue";
import OrderCard from "./OrderCard.vue";
import CallasFixControl from "./CallasFixControl.vue";
import FixComparison from "./FixComparison.vue";
import { api } from "../services/api.js";
import { UNCONFIRMED_NOTE, availableFixes, blackFixAvailable, fontLabel, iccOff, prepressNotices, unconfirmedFixes } from "./prepress.js";

const mounted = [];
function mountComponent(component, props) {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(component, props);
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return host;
}
const card = (order) => mountComponent(OrderCard, { order: { order_id: "1", customer_id: "2", status: "passed", errors: [], files: [], ...order }, runId: "run-1", selected: false, reasons: [] });
const flush = async () => { for (let i = 0; i < 6; i += 1) { await Promise.resolve(); await nextTick(); } };

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, status: 200, blob: async () => new Blob(["x"]) })));
});
afterEach(() => {
  mounted.splice(0).forEach((unmount) => unmount());
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

const fontsBlock = (extra = {}) => ({ status: "ok", count: 2, not_embedded: 1, converted: false, fonts: [
  { name: "BirchCTT", type: "TrueType", embedded: true, invisible_only: false },
  { name: "Helvetica", type: "Type1", embedded: false, invisible_only: false },
], ...extra });

describe("ICC off badge", () => {
  it("shows ICC off only when ICC was present and removed by the conversion", async () => {
    const host = card({ color_conversion: { status: "converted", icc_removed: true, icc_pages: [1] } });
    await nextTick();
    const badge = host.querySelector(".icc-off-indicator");
    expect(badge?.textContent).toBe("ICC off");
    expect(badge?.title).toContain("ICC-профиль снят, цвет переведён в DeviceCMYK");
    expect(host.querySelector(".rgb-cmyk-indicator")).not.toBeNull(); // старый бейдж RGB остаётся
  });

  it("is absent without ICC or for failed conversion", async () => {
    const plain = card({ color_conversion: { status: "converted", icc_removed: false } });
    const failed = card({ color_conversion: { status: "failed", icc_removed: true } });
    await nextTick();
    expect(plain.querySelector(".icc-off-indicator")).toBeNull();
    expect(failed.querySelector(".icc-off-indicator")).toBeNull();
  });

  it("shows the badge whenever the backend says the profile was removed, even without a converted status", () => {
    expect(iccOff({ color_conversion: { icc_removed: true, icc_pages: [2] } })).toContain("стр. 2");
    expect(iccOff({ color_conversion: { status: "failed", icc_removed: true } })).toBeNull();
    expect(iccOff({ color_conversion: { status: "converted", icc_removed: false } })).toBeNull();
    expect(iccOff({})).toBeNull();
  });

  it("reports a removed output profile (OutputIntent) without ICC objects", () => {
    const text = iccOff({ color_conversion: { status: "converted", icc_removed: true, icc_pages: [], output_intent_removed: true } });
    expect(text).toContain("выходной профиль (OutputIntent) удалён");
    expect(text).not.toContain("ICC-профиль снят");
  });

  it("stays truthful about pages where ICC remained", () => {
    const text = iccOff({ color_conversion: { status: "converted", icc_removed: true, icc_pages: [1, 2], icc_remaining_pages: [2] } });
    expect(text).toContain("(стр. 1)");
    expect(text).toContain("ICC остался на стр. 2");
  });
});

describe("fonts field", () => {
  it("shows the number and the list of unique fonts with the embedded flag", async () => {
    const host = card({ prepress: { fonts: fontsBlock() } });
    await nextTick();
    const row = host.querySelector(".fonts-row");
    expect(row?.textContent).toContain("Шрифты");
    expect(row?.textContent).toContain("2");
    expect(row?.textContent).toContain("BirchCTT (TrueType, встроен)");
    expect(row?.textContent).toContain("Helvetica (Type1, не встроен)");
    expect(host.querySelector(".fonts-outlined-indicator")).toBeNull();
    expect(host.querySelector("[data-note='fonts_not_embedded']")?.textContent).toContain("перевод в кривые невозможен");
  });

  it("with zero live fonts after outlining shows only the icon with a tooltip, no row and no button", async () => {
    const host = card({ pdf_path: "/p.pdf", prepress: { fonts: { status: "ok", count: 0, fonts: [], not_embedded: 0, converted: true } } });
    await nextTick();
    expect(host.querySelector(".fonts-row")).toBeNull();
    expect(host.querySelector(".fonts-outlined-indicator")?.title).toBe("Шрифты переведены в кривые");
    expect(host.querySelector(".callas-fix-fonts_to_outlines")).toBeNull();
  });

  it("offers the outlines button only when there are fonts to convert", async () => {
    const withFonts = card({ pdf_path: "/p.pdf", prepress: { fonts: fontsBlock() } });
    const noFonts = card({ pdf_path: "/p.pdf", prepress: { fonts: { status: "ok", count: 0, fonts: [], converted: false } } });
    const allMissing = card({ pdf_path: "/p.pdf", prepress: { fonts: fontsBlock({ count: 1, not_embedded: 1 }) } });
    await nextTick();
    expect(withFonts.querySelector(".callas-fix-fonts_to_outlines")?.textContent).toBe("Перевести шрифты в кривые");
    expect(noFonts.querySelector(".callas-fix-fonts_to_outlines")).toBeNull();
    expect(allMissing.querySelector(".callas-fix-fonts_to_outlines")).toBeNull();
  });
});

describe("grouped prepress notices and buttons", () => {
  const prepress = {
    fonts: fontsBlock({ count: 1, not_embedded: 0, fonts: [{ name: "A", type: "TrueType", embedded: true }] }),
    high_ppi: { status: "ok", count: 3, message: "Растры выше 500 ppi: 3 объекта, стр. 1, 2 (до 1200 ppi).", one_bit_message: "Однобитные растры выше 500 ppi: 1 объект, стр. 2: для штриха это обычно норма." },
    black_solid: { status: "ok", detected: true, message: "Плашка 100% K: 50% площади, стр. 1." },
  };
  const clipGroup = { code: "action:2079", source: "pitstop", severity: "warning", fix_plan: "remove_clipping", message: "Обтравка: Clipping objects found (PitStop) — 3 объекта на страницах 1, 2", count: 3, pages: [1, 2] };

  it("renders ppi, one-bit, black solid and clipping notices once each", async () => {
    const host = card({ pdf_path: "/p.pdf", prepress, issue_groups: [clipGroup] });
    await nextTick();
    const notes = [...host.querySelectorAll(".prepress-notes li")].map((li) => li.dataset.note);
    expect(notes).toEqual(["high_ppi", "one_bit_ppi", "black_solid", "clipping"]);
    expect(host.querySelector("[data-note='high_ppi']")?.textContent).toContain("Растры выше 500 ppi: 3 объекта");
    expect(host.querySelector("[data-note='clipping']")?.textContent).toContain("Enfocus PitStop");
    expect(host.querySelector("[data-note='clipping']")?.textContent).toContain("страницах 1, 2");
  });

  it("shows the black and clipping buttons with limitation tooltips", async () => {
    const host = card({ pdf_path: "/p.pdf", prepress, issue_groups: [clipGroup] });
    await nextTick();
    const black = host.querySelector(".callas-fix-black_to_rich");
    expect(black?.textContent).toBe("Перевести плашку в составной чёрный");
    expect(black?.title).toContain("мелкий текст и штрихкоды");
    const clip = host.querySelector(".callas-fix-remove_clipping");
    expect(clip?.textContent).toBe("Удалить обтравку");
    expect(clip?.title).toContain("RemoveClipPathes");
    expect(clip?.title).toContain("скрытое содержимое");
  });

  it("has no black button below the threshold and warns about manual check for a raster field", async () => {
    const host = card({ pdf_path: "/p.pdf", prepress: { black_solid: { status: "ok", detected: false, manual_check: true, message: "Крупное чёрное поле, стр. 1, не является чистым K100: скорректируйте вручную." } } });
    await nextTick();
    expect(host.querySelector(".callas-fix-black_to_rich")).toBeNull();
    expect(host.querySelector("[data-note='black_manual']")?.textContent).toContain("скорректируйте вручную");
  });

  it("offers the black button for a small pure K100 area below the solid threshold", async () => {
    const black = { status: "ok", detected: false, fix_available: true, fix_pages: [1], message: "Есть чистый K100: 3.0% площади, стр. 1. Можно перевести в составной чёрный (C60 M40 Y40 K100)." };
    const host = card({ pdf_path: "/p.pdf", prepress: { black_solid: black } });
    await nextTick();
    expect(host.querySelector(".callas-fix-black_to_rich")?.textContent).toBe("Перевести плашку в составной чёрный");
    const note = host.querySelector("[data-note='black_solid_minor']");
    expect(note?.textContent).toContain("Есть чистый K100");
    expect(prepressNotices({ prepress: { black_solid: black } })[0].level).toBe("info");
  });

  it("still offers the black button when the K100 detector did not run, but not when it found nothing", () => {
    expect(blackFixAvailable({ status: "failed", error: "separations отключены" })).toBe(true);
    expect(blackFixAvailable({ status: "skipped" })).toBe(true);
    expect(blackFixAvailable({ status: "ok", detected: false, fix_available: false })).toBe(false);
    expect(blackFixAvailable(undefined)).toBe(false);
    expect(availableFixes({ prepress: { black_solid: { status: "failed" } } })).toEqual(["black_to_rich"]);
    expect(availableFixes({ prepress: { black_solid: { status: "ok", detected: false, manual_check: true } } })).toEqual([]);
  });

  it("marks the black button as unconfirmed with a note when the K100 check failed or was skipped", async () => {
    expect(UNCONFIRMED_NOTE).toBe("Проверка K100 недоступна — кнопка показана без подтверждения наличия плашки");
    expect(unconfirmedFixes({ prepress: { black_solid: { status: "failed" } } })).toEqual(["black_to_rich"]);
    expect(unconfirmedFixes({ prepress: { black_solid: { status: "skipped" } } })).toEqual(["black_to_rich"]);
    expect(unconfirmedFixes({ prepress: { black_solid: { status: "ok", detected: true } } })).toEqual([]);
    expect(unconfirmedFixes({ prepress: { black_solid: { status: "ok", fix_available: true } } })).toEqual([]);
    const failed = card({ pdf_path: "/p.pdf", prepress: { black_solid: { status: "failed", error: "x" } } });
    const confirmed = card({ pdf_path: "/p.pdf", prepress: { black_solid: { status: "ok", detected: true, message: "Плашка 100% K: 50% площади, стр. 1." } } });
    await nextTick();
    const button = failed.querySelector(".callas-fix-black_to_rich");
    expect(button?.classList.contains("unconfirmed")).toBe(true);
    expect(button?.title).toContain(UNCONFIRMED_NOTE);
    expect(failed.querySelector("[data-note='fix_unconfirmed']")?.textContent).toBe(UNCONFIRMED_NOTE);
    const normal = confirmed.querySelector(".callas-fix-black_to_rich");
    expect(normal?.classList.contains("unconfirmed")).toBe(false);
    expect(normal?.title).not.toContain(UNCONFIRMED_NOTE);
    expect(confirmed.querySelector("[data-note='fix_unconfirmed']")).toBeNull();
  });

  it("ignores failed checks for notices and the other buttons", () => {
    const order = { prepress: { fonts: { status: "failed", error: "x" }, high_ppi: { status: "failed" }, black_solid: { status: "failed" } } };
    expect(prepressNotices(order)).toEqual([]);
    expect(availableFixes(order)).toEqual(["black_to_rich"]);
    expect(fontLabel({ name: "N", type: "Type3", embedded: null, invisible_only: true })).toBe("N (Type3, невидимый)");
  });
});

describe("CallasFixControl extra buttons", () => {
  it("start the matching plan and are hidden while processing", async () => {
    const start = vi.spyOn(api, "startCallasFix").mockResolvedValue({ comparison: { status: "processing", plan: { key: "remove_clipping", title: "Удаление обтравки" } } });
    const host = mountComponent(CallasFixControl, { runId: "run-1", orderId: "agg-1", fixes: ["fonts_to_outlines", "remove_clipping", "unknown"] });
    await nextTick();
    const buttons = host.querySelectorAll(".callas-extra-fix");
    expect(buttons.length).toBe(2);
    host.querySelector(".callas-fix-remove_clipping").click();
    await flush();
    expect(start).toHaveBeenCalledWith("run-1", "agg-1", "remove_clipping", "");
    expect(host.querySelectorAll(".callas-extra-fix").length).toBe(0);
  });
});

describe("FixComparison post check", () => {
  it("shows the plan warning, partial failure and recheck messages", async () => {
    const host = mountComponent(FixComparison, { comparison: {
      status: "ready", plan: { key: "black_to_rich", title: "Плашка" }, original_url: "/o", result_url: "/r", overlay_url: "/v",
      summary: {
        plan_warning: "Профиль меняет ВСЕ объекты с чистым K100.",
        partial_failure: "Шрифты не встроены, перевод в кривые невозможен, нужна проверка макета.",
        post_check: { level: "warning", messages: ["Чистый K100 (площадь): было 50%, стало 0%."] },
      },
    } });
    await nextTick();
    expect(host.querySelector(".fix-plan-warning")?.textContent).toContain("ВСЕ объекты");
    expect(host.querySelector(".fix-partial")?.textContent).toContain("Шрифты не встроены");
    expect(host.querySelector(".fix-post-check.post-warning")?.textContent).toContain("было 50%, стало 0%");
  });

  it("shows the failed fonts message with a retry button", async () => {
    const host = mountComponent(FixComparison, { comparison: { status: "failed", plan: { key: "fonts_to_outlines", title: "Шрифты" }, error: "Шрифты не встроены, перевод в кривые невозможен, нужна проверка макета.", summary: {} } });
    await nextTick();
    expect(host.querySelector(".fix-error")?.textContent).toContain("Шрифты не встроены, перевод в кривые невозможен");
  });
});
