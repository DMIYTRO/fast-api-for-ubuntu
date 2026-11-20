import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApp, nextTick } from "vue";
import OrderCard from "./OrderCard.vue";
import CallasFixControl from "./CallasFixControl.vue";
import FixComparison from "./FixComparison.vue";
import { api } from "../services/api.js";
import { ALL_FIXES, NOT_DETECTED_NOTE, availableFixes, blackFixAvailable, callasSourceFixTitle, fontLabel, iccOff, prepressDisplay, prepressNotices, recommendedFixes, sizeFixBadge } from "./prepress.js";

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
  prepressDisplay.showInfoNotes = true;  // тексты справочных строк проверяются при включённом показе
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve({ ok: true, status: 200, blob: async () => new Blob(["x"]) })));
});
afterEach(() => {
  prepressDisplay.showInfoNotes = false;
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

  it("is shown as inactive without ICC or for failed conversion", async () => {
    const plain = card({ color_conversion: { status: "converted", icc_removed: false } });
    const failed = card({ color_conversion: { status: "failed", icc_removed: true } });
    await nextTick();
    for (const host of [plain, failed]) {
      const badge = host.querySelector(".icc-off-indicator");
      expect(badge).not.toBeNull();
      expect(badge.classList.contains("inactive")).toBe(true);
      expect(badge.title).toBe("ICC-профиль не снимался");
    }
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
    const outlined = host.querySelector(".fonts-outlined-indicator");
    expect(outlined?.classList.contains("inactive")).toBe(true);
    expect(outlined?.title).toBe("Шрифты в кривые не переводились");
    expect(host.querySelector("[data-note='fonts_not_embedded']")?.textContent).toContain("перевод в кривые невозможен");
  });

  it("with zero live fonts after outlining shows the active icon, no row, and a non-recommended button", async () => {
    const host = card({ pdf_path: "/p.pdf", prepress: { fonts: { status: "ok", count: 0, fonts: [], not_embedded: 0, converted: true } } });
    await nextTick();
    expect(host.querySelector(".fonts-row")).toBeNull();
    const outlined = host.querySelector(".fonts-outlined-indicator");
    expect(outlined?.title).toBe("Шрифты переведены в кривые");
    expect(outlined?.classList.contains("inactive")).toBe(false);
    const button = host.querySelector(".callas-fix-fonts_to_outlines");
    expect(button).not.toBeNull();
    expect(button.classList.contains("recommended")).toBe(false);
  });

  it("recommends the outlines button only when there are fonts to convert", async () => {
    const withFonts = card({ pdf_path: "/p.pdf", prepress: { fonts: fontsBlock() } });
    const noFonts = card({ pdf_path: "/p.pdf", prepress: { fonts: { status: "ok", count: 0, fonts: [], converted: false } } });
    const allMissing = card({ pdf_path: "/p.pdf", prepress: { fonts: fontsBlock({ count: 1, not_embedded: 1 }) } });
    await nextTick();
    const recommended = withFonts.querySelector(".callas-fix-fonts_to_outlines");
    expect(recommended?.textContent).toContain("Перевести шрифты в кривые");
    expect(recommended.classList.contains("recommended")).toBe(true);
    expect(recommended.querySelector(".fix-recommended-dot")).not.toBeNull();
    expect(recommended.title).not.toContain(NOT_DETECTED_NOTE);
    for (const host of [noFonts, allMissing]) {
      const button = host.querySelector(".callas-fix-fonts_to_outlines");
      expect(button).not.toBeNull();
      expect(button.disabled).toBe(false);
      expect(button.classList.contains("recommended")).toBe(false);
      expect(button.querySelector(".fix-recommended-dot")).toBeNull();
      expect(button.title.startsWith(NOT_DETECTED_NOTE)).toBe(true);
    }
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

  it("keeps the black button non-recommended below the threshold and warns about manual check for a raster field", async () => {
    const host = card({ pdf_path: "/p.pdf", prepress: { black_solid: { status: "ok", detected: false, manual_check: true, message: "Крупное чёрное поле, стр. 1, не является чистым K100: скорректируйте вручную." } } });
    await nextTick();
    const black = host.querySelector(".callas-fix-black_to_rich");
    expect(black).not.toBeNull();
    expect(black.classList.contains("recommended")).toBe(false);
    expect(black.title.startsWith(NOT_DETECTED_NOTE)).toBe(true);
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

  it("recommends the black button only when the K100 check found something", () => {
    expect(blackFixAvailable({ status: "ok", detected: true })).toBe(true);
    expect(blackFixAvailable({ status: "ok", detected: false, fix_available: true })).toBe(true);
    expect(blackFixAvailable({ status: "failed", error: "separations отключены" })).toBe(false);
    expect(blackFixAvailable({ status: "skipped" })).toBe(false);
    expect(blackFixAvailable({ status: "ok", detected: false, fix_available: false })).toBe(false);
    expect(blackFixAvailable(undefined)).toBe(false);
    expect(recommendedFixes({ prepress: { black_solid: { status: "failed" } } })).toEqual([]);
    expect(recommendedFixes({ prepress: { black_solid: { status: "ok", detected: false, manual_check: true } } })).toEqual([]);
    expect(recommendedFixes({ prepress: { black_solid: { status: "ok", detected: true } } })).toEqual(["black_to_rich"]);
  });

  it("does not recommend the black button when the K100 check failed, but still renders it enabled", async () => {
    const failed = card({ pdf_path: "/p.pdf", prepress: { black_solid: { status: "failed", error: "x" } } });
    const confirmed = card({ pdf_path: "/p.pdf", prepress: { black_solid: { status: "ok", detected: true, message: "Плашка 100% K: 50% площади, стр. 1." } } });
    await nextTick();
    const button = failed.querySelector(".callas-fix-black_to_rich");
    expect(button?.disabled).toBe(false);
    expect(button?.classList.contains("recommended")).toBe(false);
    expect(button?.title.startsWith(NOT_DETECTED_NOTE)).toBe(true);
    expect(failed.querySelector("[data-note='fix_unconfirmed']")).toBeNull();
    const normal = confirmed.querySelector(".callas-fix-black_to_rich");
    expect(normal?.classList.contains("recommended")).toBe(true);
    expect(normal?.title).not.toContain(NOT_DETECTED_NOTE);
  });

  it("renders all fix buttons with an svg icon and enabled", async () => {
    expect(availableFixes({})).toEqual(ALL_FIXES);
    const host = card({ pdf_path: "/p.pdf" });
    await nextTick();
    for (const key of ALL_FIXES) {
      const button = host.querySelector(`.callas-fix-${key}`);
      expect(button, key).not.toBeNull();
      expect(button.querySelector("svg"), key).not.toBeNull();
      expect(button.disabled, key).toBe(false);
      expect(button.classList.contains("recommended"), key).toBe(false);
    }
    const noPdf = card({});
    await nextTick();
    expect(noPdf.querySelector(".callas-extra-fix")).toBeNull();
  });

  it("shows the downsample button always and recommends it only when high-ppi rasters exist", async () => {
    const withPpi = card({ pdf_path: "/p.pdf", prepress: { high_ppi: { status: "ok", count: 2, pages: [1], max_ppi: 900, message: "m" } } });
    const clean = card({ pdf_path: "/p.pdf", prepress: { high_ppi: { status: "ok", count: 0, pages: [] } } });
    await nextTick();
    const hot = withPpi.querySelector(".callas-fix-downsample_images");
    expect(hot?.textContent).toContain("Уменьшить разрешение до 300 ppi");
    expect(hot.classList.contains("recommended")).toBe(true);
    expect(hot.querySelector("svg")).not.toBeNull();
    expect(hot.title).toContain("уменьшит до 300 ppi все изображения выше 300 ppi; штриховые 1-бит проверьте по превью");
    const cold = clean.querySelector(".callas-fix-downsample_images");
    expect(cold).not.toBeNull();
    expect(cold.disabled).toBe(false);
    expect(cold.classList.contains("recommended")).toBe(false);
    expect(recommendedFixes({ prepress: { high_ppi: { status: "ok", count: 1 } } })).toEqual(["downsample_images"]);
    expect(recommendedFixes({ prepress: { high_ppi: { status: "ok", count: 0 } } })).toEqual([]);
  });

  it("ignores failed checks for notices and the other buttons", () => {
    const order = { prepress: { fonts: { status: "failed", error: "x" }, high_ppi: { status: "failed" }, black_solid: { status: "failed" } } };
    expect(prepressNotices(order)).toEqual([]);
    expect(recommendedFixes(order)).toEqual([]);
    expect(availableFixes(order)).toEqual(ALL_FIXES);
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

describe("callasSourceFixTitle", () => {
  it("is empty without accepted source fixes", () => {
    expect(callasSourceFixTitle({ files: [{ name: "a.pdf" }, { name: "b.pdf", callas_source_fix: { status: "ready" } }] })).toBe("");
  });
  it("names each accepted fix and whether only bleed was drawn", () => {
    const title = callasSourceFixTitle({ files: [
      { name: "face.pdf", callas_source_fix: { status: "accepted", plan: "size_correction", bleed_only: true } },
      { name: "back.tif", callas_source_fix: { status: "accepted", plan: "size_correction", bleed_only: false } },
    ] });
    expect(title).toContain("face.pdf: Callas дорисовал поля вылета, макет не изменён");
    expect(title).toContain("back.tif: размер исправлен Callas, макет изменён");
  });
  it("shows the badge on the order card", async () => {
    const host = card({ files: [{ id: "f", name: "face.pdf", side: "face", callas_source_fix: { status: "accepted", plan: "size_correction", bleed_only: true } }] });
    await flush();
    const badge = host.querySelector(".callas-source-fix-indicator");
    expect(badge?.textContent).toBe("Поля Callas");
    expect(badge?.getAttribute("title")).toContain("face.pdf");
  });
});

describe("sizeFixBadge", () => {
  const fix = (method, extra = {}) => ({ method, auto: true, from_mm: [100, 50], to_mm: [102, 52], detail: "деталь", ...extra });
  it("is null without any size correction", () => {
    expect(sizeFixBadge({ files: [{ name: "a.pdf" }, { name: "b.pdf", callas_source_fix: { status: "ready" } }] })).toBeNull();
    expect(sizeFixBadge(null)).toBeNull();
  });
  it.each([
    ["bleed", "Поля Callas"], ["reveal", "Поля открыты"], ["stretch", "Растянуто"], ["shrink", "Уменьшено"], ["fill", "Подогнано"],
    ["canvas", "Поля/обрезка"],
  ])("labels method %s", (method, label) => {
    expect(sizeFixBadge({ files: [{ name: "a.pdf", size_fix: fix(method) }] }).label).toBe(label);
  });
  it("keeps one label when several files share a method", () => {
    const badge = sizeFixBadge({ files: [{ name: "a.pdf", size_fix: fix("fill") }, { name: "b.pdf", size_fix: fix("fill") }] });
    expect(badge.label).toBe("Подогнано");
    expect(badge.title).toContain("a.pdf");
    expect(badge.title).toContain("b.pdf");
  });
  it("uses a generic label for mixed methods", () => {
    expect(sizeFixBadge({ files: [{ name: "a.pdf", size_fix: fix("reveal") }, { name: "b.tif", size_fix: fix("fill") }] }).label).toBe("Размер скорректирован");
  });
  it("describes each file with sizes and who decided", () => {
    const badge = sizeFixBadge({ files: [
      { name: "a.pdf", size_fix: fix("stretch", { detail: "растянуто на 0,8%", from_mm: [100, 50.5], to_mm: [100.6, 51] }) },
      { name: "b.pdf", size_fix: fix("shrink", { auto: false, detail: "сжато до формата" }) },
    ] });
    expect(badge.title).toContain("a.pdf: растянуто на 0,8%, 100×50.5→100.6×51 мм (автоматически)");
    expect(badge.title).toContain("b.pdf: сжато до формата, 100×50→102×52 мм (подтверждено)");
  });
  it("merges an accepted callas fix with size_fix bleed into one badge", () => {
    const badge = sizeFixBadge({ files: [{ name: "face.pdf", size_fix: fix("bleed"), callas_source_fix: { status: "accepted", plan: "size_correction", bleed_only: true } }] });
    expect(badge.label).toBe("Поля Callas");
    expect(badge.title).toContain("face.pdf: деталь, 100×50→102×52 мм (автоматически)");
    expect(badge.title).toContain("Callas дорисовал поля вылета, макет не изменён");
    expect(badge.title.match(/face\.pdf/g)).toHaveLength(1);
  });
  it("still shows «Поля Callas» for a manually accepted fix without size_fix", () => {
    const badge = sizeFixBadge({ files: [{ name: "face.pdf", callas_source_fix: { status: "accepted", plan: "size_correction", bleed_only: true } }] });
    expect(badge.label).toBe("Поля Callas");
    expect(badge.title).toContain("face.pdf: Callas дорисовал поля вылета");
  });
  it("renders a single badge on the order card", async () => {
    const host = card({ files: [
      { id: "f", name: "face.pdf", side: "face", size_fix: fix("reveal", { detail: "открыты скрытые поля" }) },
      { id: "b", name: "back.pdf", side: "back", size_fix: fix("reveal") },
    ] });
    await flush();
    const badges = host.querySelectorAll(".callas-source-fix-indicator");
    expect(badges).toHaveLength(1);
    expect(badges[0].textContent).toBe("Поля открыты");
    expect(badges[0].getAttribute("aria-label")).toContain("face.pdf: открыты скрытые поля");
  });
  it("renders no duplicate badge for bleed size_fix plus accepted callas fix", async () => {
    const host = card({ files: [{ id: "f", name: "face.pdf", side: "face", size_fix: fix("bleed"), callas_source_fix: { status: "accepted", plan: "size_correction", bleed_only: true } }] });
    await flush();
    expect(host.querySelectorAll(".callas-source-fix-indicator")).toHaveLength(1);
  });
});

describe("sizeFixBadge for other Callas plans", () => {
  it("does not call a fonts fix «Поля Callas»", () => {
    const badge = sizeFixBadge({ files: [{ name: "face.pdf", callas_source_fix: { status: "accepted", plan: "fonts_to_outlines", plan_title: "Перевод шрифтов в кривые" } }] });
    expect(badge.label).toBe("Исправлено Callas");
    expect(badge.title).toContain("Перевод шрифтов в кривые");
  });
});
