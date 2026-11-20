import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createApp, nextTick } from "vue";
import OrderCard from "./OrderCard.vue";
import { prepressDisplay } from "./prepress.js";

const mounted = [];
function mount(order) {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(OrderCard, {
    order,
    runId: "run-1",
    selected: false,
    reasons: [],
  });
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return host;
}

// Содержимое справочных строк проверяется с включённым показом; по умолчанию они скрыты.
beforeEach(() => { prepressDisplay.showInfoNotes = true; });
afterEach(() => { mounted.splice(0).forEach((unmount) => unmount()); prepressDisplay.showInfoNotes = false; });

describe("OrderCard processing failures", () => {
  it("shows PDF processing errors and explains why PitStop has no report", async () => {
    const host = mount({
      order_id: "26128104",
      customer_id: "22557",
      status: "error",
      pitstop_status: "not_checked",
      errors: [],
      processing_errors: ["PDF: Ghostscript не смог создать файл"],
      files: [],
    });
    await nextTick();

    expect(host.querySelector(".issue-list.errors")?.textContent)
      .toContain("PDF: Ghostscript не смог создать файл");
    expect(host.querySelector(".pitstop-skipped")?.textContent)
      .toContain("PitStop не проверял этот заказ");
    expect(host.querySelector(".pitstop-skipped")?.textContent)
      .toContain("успешного создания итогового PDF");
  });

  it("shows the Callas fix block only when there is a PDF or a comparison", async () => {
    const plain = mount({ order_id: "1", status: "passed", files: [] });
    const withPdf = mount({ order_id: "2", status: "passed", files: [], pdf_url: "/api/orders/2/pdf" });
    const withFix = mount({
      order_id: "3", status: "passed", files: [],
      callas_fix: { comparison: { status: "failed", plan: { key: "size", title: "Коррекция размера" }, error: "boom" } },
    });
    await nextTick();
    expect(plain.querySelector(".callas-fix")).toBeNull();
    expect(withPdf.querySelector("button.callas-size-fix")).not.toBeNull();
    expect(withFix.querySelector(".fix-comparison")?.textContent).toContain("Коррекция размера");
  });
});

describe("OrderCard issue grouping", () => {
  it("renders backend issue_groups with counts and expandable details", async () => {
    const host = mount({
      order_id: "1", customer_id: "2", status: "error", files: [],
      errors: ["x", "x"],
      issue_groups: [
        { code: "low_dpi", severity: "error", source: "local", message: "Низкое разрешение — 2 повтора в 2 файлах", count: 2, objects: null, pages: [], files: ["a_face.tif", "a_back.tif"], samples: ["разрешение 150 DPI", "разрешение 200 DPI"] },
        { code: "mixed_formats", severity: "warning", source: "local", message: "стороны заказа имеют разные форматы: PDF, TIFF", count: 1, objects: null, pages: [], files: [], samples: [] },
        { code: "action:2078", severity: "error", source: "pitstop", message: "RGB color is used — 3 объекта", count: 1, objects: 3, pages: [1], files: [], samples: [] },
      ],
    });
    await nextTick();
    const errors = host.querySelector(".issue-list.errors");
    expect(errors.querySelectorAll("li.issue-group")).toHaveLength(1);
    expect(errors.textContent).toContain("Низкое разрешение — 2 повтора в 2 файлах");
    expect(errors.textContent).toContain("×2");
    expect(errors.textContent).toContain("Файлы: a_face.tif, a_back.tif");
    expect(errors.textContent).toContain("разрешение 150 DPI");
    expect(errors.textContent).not.toContain("RGB color is used");
    expect(host.querySelector(".issue-list.warnings").textContent).toContain("стороны заказа имеют разные форматы");
  });

  it("groups raw messages by type when issue_groups is missing", async () => {
    const host = mount({
      order_id: "1", status: "error", files: [],
      errors: ["размер 90.2x50.1 мм; ожидается 94.0x54.0 мм", "размер 88.0x48.0 мм; ожидается 94.0x54.0 мм"],
    });
    await nextTick();
    const items = host.querySelectorAll(".issue-list.errors li.issue-group");
    expect(items).toHaveLength(1);
    expect(items[0].textContent).toContain("Размер не соответствует ожидаемому — 2 повтора");
  });
});

describe("OrderCard TIFF flatten result", () => {
  it("shows flatten icon only for successful processing metadata", async () => {
    const pending = mount({ order_id: "1", files: [{ name: "layered.tif", has_unflattened_layers: true }] });
    const completed = mount({ order_id: "2", files: [{
      name: "layered.tif", has_unflattened_layers: true, tiff_flattened: true,
      tiff_visible_layer_count: 2, tiff_hidden_layer_count: 1,
    }] });
    await nextTick();
    expect(pending.querySelector(".tiff-flatten-indicator")).toBeNull();
    expect(completed.querySelector(".tiff-flatten-indicator")?.textContent).toContain("TIFF сведён");
    expect(completed.querySelector(".tiff-flatten-indicator")?.title).toContain("видимых слоёв 2, скрытых 1");
  });
});

describe("OrderCard RGB to CMYK badge", () => {
  const base = { order_id: "1", customer_id: "2", status: "passed", errors: [], files: [] };

  it("shows the active RGB badge when the color model was converted", async () => {
    const host = mount({ ...base, color_conversion: { status: "converted", profile: "P", pages: [1, 2] } });
    await nextTick();
    const badge = host.querySelector(".rgb-cmyk-indicator");
    expect(badge?.textContent).toBe("RGB");
    expect(badge?.classList.contains("inactive")).toBe(false);
    expect(badge?.title).toContain("RGB переведён в CMYK");
    expect(badge?.title).toContain("1, 2");
  });

  it("shows an inactive badge without conversion or after a failed conversion", async () => {
    const none = mount(base);
    const failed = mount({ ...base, color_conversion: { status: "failed", error: "x" } });
    await nextTick();
    for (const host of [none, failed]) {
      const badge = host.querySelector(".rgb-cmyk-indicator");
      expect(badge).not.toBeNull();
      expect(badge.classList.contains("inactive")).toBe(true);
      expect(badge.title).toBe("RGB → CMYK не выполнялся");
    }
  });
});

describe("OrderCard expected size", () => {
  const file = (extra) => ({
    name: "06_NP(K)_(SRA3)_(434x314)_4-0_(3773-1)_1-face.pdf",
    parsed: { side: "face", width_mm: 434, height_mm: 314, front_colors: 4, back_colors: 0 },
    ...extra,
  });
  const order = (files) => ({ order_id: "1", customer_id: "3773", status: "passed", errors: [], files });

  it("shows the backend reference size (SRA3 already includes bleed)", async () => {
    const host = mount(order([file({ expected_mm: [314, 434] })]));
    await nextTick();
    expect(host.textContent).toContain("314.0 × 434.0 мм");
    expect(host.textContent).not.toContain("438.0 × 318.0 мм");
  });

  it("falls back to format + 4 mm for old runs without expected_mm", async () => {
    const host = mount(order([file({})]));
    await nextTick();
    expect(host.textContent).toContain("438.0 × 318.0 мм");
  });
});

describe("OrderCard kit badge", () => {
  it("shows the kit position and its main order", async () => {
    const host = mount({
      order_id: "26234882", customer_id: "36056", status: "warning", errors: [], files: [],
      kit: { main_id: "26234881", member_ids: [], position: 2, size: 19 },
    });
    await nextTick();
    const badge = host.querySelector(".kit-indicator");
    expect(badge?.textContent).toContain("Комплект 2/19 · 26234881");
    expect(badge?.getAttribute("title")).toContain("возвращает в Sborka весь комплект");
  });

  it("has no badge outside a kit", async () => {
    const host = mount({ order_id: "1", customer_id: "2", status: "passed", errors: [], files: [] });
    await nextTick();
    expect(host.querySelector(".kit-indicator")).toBeNull();
  });
});

describe("OrderCard check policy", () => {
  const base = { order_id: "1", customer_id: "2", status: "passed", errors: [], files: [] };

  it("shows the policy label as a badge and hides it for old runs", async () => {
    const basic = mount({ ...base, check_policy: { key: "basic", label: "Облегчённая проверка (Input)", reason: "input_root" } });
    const full = mount({ ...base, check_policy: { key: "full", label: "Полная проверка (CorelDRAW)", reason: "strict_folder" } });
    const old = mount(base);
    await nextTick();
    expect(basic.querySelector(".check-policy-indicator")?.textContent).toBe("Облегчённая проверка (Input)");
    expect(basic.querySelector(".check-policy-indicator")?.classList.contains("policy-basic")).toBe(true);
    expect(full.querySelector(".check-policy-indicator")?.textContent).toBe("Полная проверка (CorelDRAW)");
    expect(old.querySelector(".check-policy-indicator")).toBeNull();
  });

  it("shows the source folder chip next to the file name in the details", async () => {
    const host = mount({ ...base, files: [{ id: "f1", filename: "a.pdf", source_folder: "CorelDRAW" }, { id: "f2", filename: "b.pdf", source_folder: "" }] });
    await nextTick();
    host.querySelector(".details-toggle").click();
    await nextTick();
    const chips = host.querySelectorAll(".source-folder-chip");
    expect(chips).toHaveLength(1);
    expect(chips[0].textContent).toBe("CorelDRAW");
  });

  it("shows a demoted clipping group as an info note, not a warning", async () => {
    const host = mount({
      ...base,
      issue_groups: [{ code: "action:2079", severity: "info", source: "pitstop", message: "Clipping objects found — 3 объекта", count: 1, samples: [], files: [], pages: [], policy_demoted: true, fix_plan: "remove_clipping" }],
    });
    await nextTick();
    const notes = host.querySelector(".prepress-notes");
    expect(notes?.classList.contains("warnings")).toBe(false);
    expect(notes?.querySelector("[data-note=clipping]")?.classList.contains("note-info")).toBe(true);
    expect(notes?.textContent).toContain("Не влияет на статус (облегчённая проверка)");
  });

  it("hides the grey info block by default: it repeats the PitStop report", async () => {
    prepressDisplay.showInfoNotes = false;
    const host = mount({
      ...base,
      color_conversion: { status: "converted", reason: "rgb", icc_removed: true },
      issue_groups: [{ code: "action:2079", severity: "info", source: "pitstop", message: "Clipping objects found — 43 объекта", count: 1, samples: [], files: [], pages: [], policy_demoted: true, fix_plan: "remove_clipping" }],
    });
    await nextTick();
    expect(host.querySelector(".prepress-notes")).toBeNull();
    expect(host.textContent).not.toContain("Допечатная обработка");
  });

  it("still shows prepress warnings when info lines are hidden", async () => {
    prepressDisplay.showInfoNotes = false;
    const host = mount({
      ...base,
      color_conversion: { status: "failed", reason: "icc", error: "callas недоступен" },
      issue_groups: [{ code: "action:2079", severity: "info", source: "pitstop", message: "Clipping objects found — 3 объекта", count: 1, samples: [], files: [], pages: [], policy_demoted: true, fix_plan: "remove_clipping" }],
    });
    await nextTick();
    const notes = host.querySelector(".prepress-notes");
    expect(notes?.classList.contains("warnings")).toBe(true);
    expect(notes?.textContent).toContain("Не удалось снять ICC-профиль: callas недоступен");
    expect(notes?.querySelector("[data-note=clipping]")).toBeNull();
  });
});

describe("OrderCard color correction lines", () => {
  const base = { order_id: "1", customer_id: "2", status: "passed", errors: [], files: [] };
  const noteText = (host) => host.querySelector(".prepress-notes")?.textContent || "";

  it("rgb conversion and old records without reason", async () => {
    const withReason = mount({ ...base, color_conversion: { status: "converted", reason: "rgb", pages: [1] } });
    const old = mount({ ...base, color_conversion: { status: "converted", pages: [1, 2] } });
    await nextTick();
    expect(noteText(withReason)).toContain("Цветовая модель: RGB → CMYK скорректирована");
    expect(noteText(old)).toContain("Цветовая модель: RGB → CMYK скорректирована");
    expect(withReason.querySelector(".prepress-notes")?.classList.contains("warnings")).toBe(false);
    expect(old.querySelector(".rgb-cmyk-indicator")?.classList.contains("inactive")).toBe(false);
  });

  it("rgb conversion with icc removed adds an ICC line", async () => {
    const host = mount({ ...base, color_conversion: { status: "converted", reason: "rgb", icc_removed: true } });
    await nextTick();
    expect(noteText(host)).toContain("Цветовая модель: RGB → CMYK скорректирована");
    expect(noteText(host)).toContain("Цветовой профиль: ICC снят");
  });

  it("icc-only conversion does not light the RGB badge", async () => {
    const host = mount({ ...base, color_conversion: { status: "converted", reason: "icc", icc_removed: true, from: "ICC", to: "DeviceCMYK", pages: [1] } });
    await nextTick();
    expect(noteText(host)).toContain("Цветовой профиль: ICC снят (CMYK без пересчёта)");
    expect(noteText(host)).not.toContain("RGB → CMYK");
    expect(host.querySelector(".rgb-cmyk-indicator")?.classList.contains("inactive")).toBe(true);
    expect(host.querySelector(".icc-off-indicator")?.classList.contains("inactive")).toBe(false);
  });

  it("skipped icc strip is a neutral note, not a warning", async () => {
    const host = mount({ ...base, color_conversion: { status: "skipped", reason: "icc", skip_reason: "lab", skip_message: "В PDF есть Lab" } });
    await nextTick();
    expect(noteText(host)).toContain("Цветовой профиль: В PDF есть Lab");
    expect(host.querySelector(".prepress-notes")?.classList.contains("warnings")).toBe(false);
  });

  it("failed conversion is an actionable warning with the error", async () => {
    const icc = mount({ ...base, color_conversion: { status: "failed", reason: "icc", error: "профиль callas недоступен" } });
    const rgb = mount({ ...base, color_conversion: { status: "failed", error: "таймаут" } });
    await nextTick();
    expect(noteText(icc)).toContain("Не удалось снять ICC-профиль: профиль callas недоступен");
    expect(icc.querySelector(".prepress-notes")?.classList.contains("warnings")).toBe(true);
    expect(noteText(rgb)).toContain("Не удалось перевести RGB в CMYK: таймаут");
  });
});
