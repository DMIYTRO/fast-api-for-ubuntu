import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createApp, h, nextTick } from "vue";
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

describe("OrderCard export", () => {
  it("shows only the PDF icon link when the order has export URLs", () => {
    const host = mount({
      order_id: "2", status: "passed", files: [],
      pdf_url: "/api/orders/2/pdf?run_id=run-1",
      html_url: "/runs/run-1/report",
      json_url: "/api/checks/run-1/export.json",
    });

    const links = host.querySelectorAll(".export-links a");
    expect(links).toHaveLength(1);
    const [link] = links;
    expect(link.getAttribute("href")).toBe("/api/orders/2/pdf?run_id=run-1");
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("aria-label")).toBe("Открыть PDF заказа");
    expect(link.querySelector("svg")).not.toBeNull();
  });
});

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

  // Колонка «Норма» скрыта (SHOW_NORM = false в OrderCard.vue) — вернуть it, когда её включат.
  it.skip("shows the backend reference size (SRA3 already includes bleed)", async () => {
    const host = mount(order([file({ expected_mm: [314, 434] })]));
    await nextTick();
    expect(host.textContent).toContain("314.0 × 434.0 мм");
    expect(host.textContent).not.toContain("438.0 × 318.0 мм");
  });

  it.skip("falls back to format + 4 mm for old runs without expected_mm", async () => {
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

describe("OrderCard customer comment", () => {
  const long = "друк з лицьової сторони\n\nколір не міняти\n  ВЕЛИКЕ_СЛОВО_" + "Ж".repeat(120);

  it("shows the full comment under the previews title and before the images, as text", async () => {
    const host = mount({
      order_id: "26252239", status: "passed", files: [],
      customer_comment: "друк з лицьової сторони",
    });
    await nextTick();

    const block = host.querySelector("[data-testid='customer-comment']");
    expect(block).not.toBeNull();
    expect(block.textContent).toContain("Комментарий заказчика");
    expect(block.querySelector(".customer-comment-text").textContent).toBe("друк з лицьової сторони");
    const column = host.querySelector(".preview-column");
    const order = [...column.children];
    expect(order[0].textContent).toContain("Превью макетов");
    expect(order[1]).toBe(block);
    expect(order[1].compareDocumentPosition(column.querySelector(".custom-preview")) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("keeps every line and the whole long text with no truncation or toggle", async () => {
    const host = mount({ order_id: "1", status: "passed", files: [], customer_comment: long });
    await nextTick();

    const text = host.querySelector(".customer-comment-text");
    expect(text.textContent).toBe(long); // all lines, blank line included, byte for byte
    expect(host.querySelector(".customer-comment button, .customer-comment a")).toBeNull();
    expect(host.querySelector(".customer-comment").textContent).not.toContain("…");
  });

  it("renders HTML-looking comment as plain text", async () => {
    const host = mount({
      order_id: "1", status: "passed", files: [],
      customer_comment: "<img src=x onerror=alert(1)><b>жирний</b>",
    });
    await nextTick();

    const text = host.querySelector(".customer-comment-text");
    expect(text.querySelector("img, b")).toBeNull();
    expect(text.textContent).toBe("<img src=x onerror=alert(1)><b>жирний</b>");
  });

  it.each([undefined, null, "", "  \n\t "])("hides the row for an empty comment (%j)", async (value) => {
    const host = mount({ order_id: "1", status: "passed", files: [], customer_comment: value });
    await nextTick();
    expect(host.querySelector("[data-testid='customer-comment']")).toBeNull();
    expect(host.textContent).not.toContain("Комментарий заказчика");
  });

  it("is available on a PitStop error and with no images", async () => {
    const host = mount({
      order_id: "26252239", status: "error", pitstop_status: "failed", files: [], preview_paths: [],
      pitstop: { execution_status: "failed", verdict: "error", technical_error: "PitStop недоступен" },
      customer_comment: "друк з лицьової сторони",
    });
    await nextTick();
    expect(host.querySelector(".customer-comment-text")?.textContent).toBe("друк з лицьової сторони");
  });

  it("appears on an already shown card when the order is updated", async () => {
    const order = { order_id: "1", status: "waiting_confirmation", files: [] };
    const host = document.createElement("div");
    document.body.append(host);
    const { reactive } = await import("vue");
    const state = reactive({ order });
    const app = createApp({ render: () => h(OrderCard, { order: state.order, runId: "run-1", selected: false, reasons: [] }) });
    app.mount(host);
    mounted.push(() => { app.unmount(); host.remove(); });
    await nextTick();
    expect(host.querySelector(".customer-comment")).toBeNull();

    state.order = { ...order, customer_comment: "пізня відповідь" };
    await nextTick();
    expect(host.querySelector(".customer-comment-text")?.textContent).toBe("пізня відповідь");
  });
});

describe("OrderCard return comment summary", () => {
  it("shows the pre-selected return reason next to the comment button", async () => {
    const host = mount({
      order_id: "26242148", customer_id: "3773", status: "error", files: [],
      return_suggestions: [{ text: "Низкое разрешение изображений", category: "Разрешение", confidence: "high", preselect: true, evidence: "" }],
    });
    await nextTick();
    const actions = host.querySelector(".order-head-actions");
    expect(actions.querySelector(".return-badge")?.textContent).toBe("1");
    expect(actions.querySelector(".return-summary-text")?.textContent).toBe("Низкое разрешение изображений");
  });
});

describe("OrderCard action result", () => {
  const base = { order_id: "26257189", customer_id: "1", status: "warning", errors: [], files: [] };

  it.each([
    [{ status: "prepared", action: "print" }, "status-ok", "Подготовлено"],
    [{ status: "review_required", action: "print", message: "Созданы PDF для недостающих сторон." }, "status-attention", "Созданы PDF из слоёв TIFF"],
    [{ status: "conflict", action: "print", message: "Файл с таким именем уже есть." }, "status-attention", "Файл с таким именем уже есть."],
    [{ status: "error", action: "print", message: "Не найден один или несколько исходных файлов заказа." }, "status-error", "Не найден один"],
    [{ status: "rejected", action: "print", message: "Заказ не прошёл проверку." }, "status-error", "Заказ не прошёл проверку."],
    [{ status: "failed", action: "print" }, "status-error", "причина не сохранена"],
  ])("renders %o with the right tone and no raw status", async (actionResult, tone, text) => {
    const host = mount({ ...base, action_result: actionResult });
    await nextTick();
    const line = host.querySelector(".action-result");
    expect(line.classList.contains(tone)).toBe(true);
    expect(line.textContent).toContain(text);
    expect(line.textContent).not.toMatch(/failed|rejected|review_required|Действие:/);
  });
});

describe("OrderCard layered TIFF review after reload", () => {
  it("loads the generated export from the stored action result", async () => {
    const { api } = await import("../services/api.js");
    const calls = [];
    const original = api.layeredTiffPreview;
    api.layeredTiffPreview = async (url) => { calls.push(url); return { status: "completed", pdf_url: "/export.pdf" }; };
    try {
      mount({
        order_id: "26257189", status: "warning", errors: [],
        files: [{ id: "f-1", name: "face.tif", side: "face", has_unflattened_layers: true, layered_tiff_export_url: "/api/layered/f-1" }],
        // Так карточку отдаёт сервер после перечитывания заказов (stored_action_result).
        action_result: { status: "review_required", action: "print", generated_layered_file_ids: ["f-1"] },
      });
      await nextTick();
      expect(calls).toEqual(["/api/layered/f-1"]);
    } finally {
      api.layeredTiffPreview = original;
    }
  });
});

describe("OrderCard postpress operations", () => {
  it("shows postpress operations next to the fold with rounding and drilling details", async () => {
    const host = mount({
      order_id: "1", status: "passed", files: [],
      postpress: {
        raw: "биговка : 1; Сгиб : 1; Рулонна ламінація …; скругление углов : 4; Сверление : 2, &empty; - 5 мм; кашировка : 2",
        fold: { type: "half-fold", count: 1, operation: "Биг", label: "Биг: 1", needs_confirmation: false },
        operations: [
          { kind: "lamination", label: "Ламинация", detail: "мат, 2 стороны, 25 мкм", text: "Рулонна ламінація …" },
          { kind: "rounding", label: "Скругление углов", count: 4, text: "скругление углов : 4" },
          { kind: "drilling", label: "Сверление", count: 2, diameter_mm: 5, text: "Сверление : 2, ∅ - 5 мм" },
          { kind: "mounting", label: "Кашировка", count: 2, text: "кашировка : 2" },
        ],
      },
    });
    await nextTick();
    const line = host.querySelector(".customer-line");
    expect(line.querySelector(".fold-indicator")?.textContent).toContain("Биг: 1");
    const ops = [...line.querySelectorAll(".postpress-indicator")];
    expect(ops.map((el) => el.querySelector("b").textContent)).toEqual([
      "Ламинация: мат, 2 стороны, 25 мкм", "Скругление углов", "Сверление", "Кашировка",
    ]);
    expect(ops[1].querySelector(".postpress-count").textContent).toBe("4 угла");
    expect(ops[2].querySelector(".postpress-count").textContent).toBe("2 отв.");
    expect(ops[2].querySelector(".postpress-diameter").textContent).toBe("Ø 5 мм");
    expect(ops[2].title).toContain("Sborka: Сверление");
    expect(ops[3].querySelector(".postpress-count").textContent).toBe("× 2");
  });

  it("shows nothing extra when Sborka sent no operations", async () => {
    const host = mount({ order_id: "2", status: "passed", files: [], postpress: { raw: "Комплект : 2" } });
    await nextTick();
    expect(host.querySelector(".postpress-indicator")).toBeNull();
  });
});

describe("OrderCard cancelled in Sborka", () => {
  it("shows a grey cancelled note instead of the order body and hides return controls", async () => {
    const host = mount({
      order_id: "26255491", status: "cancelled", files: [], errors: ["заказ отменён клиентом в Сборке: проверка не выполнялась"],
      sborka_cancelled: { sborka_status: "14", moved_to: "/in/Cancelled/26255491", moved_files: ["a.pdf"] },
    });
    await nextTick();
    expect(host.querySelector(".order-status").textContent).toBe("Отменён клиентом");
    expect(host.querySelector(".sborka-cancelled").textContent).toContain("/in/Cancelled/26255491");
    expect(host.querySelector(".order-body")).toBeNull();
    expect(host.querySelector(".paid-design")).toBeNull();
  });

  it("shows the move error when the files stayed in place", async () => {
    const host = mount({ order_id: "1", status: "cancelled", files: [], sborka_cancelled: { moved_to: "/x", move_error: "a.pdf: Permission denied" } });
    await nextTick();
    expect(host.querySelector(".sborka-cancelled-error").textContent).toContain("Permission denied");
  });
});
