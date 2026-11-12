import { afterEach, describe, expect, it } from "vitest";
import { createApp, nextTick } from "vue";
import OrderCard from "./OrderCard.vue";

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

afterEach(() => mounted.splice(0).forEach((unmount) => unmount()));

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

  it("shows the RGB badge only when the color model was converted", async () => {
    const host = mount({ ...base, color_conversion: { status: "converted", profile: "P", pages: [1, 2] } });
    await nextTick();
    const badge = host.querySelector(".rgb-cmyk-indicator");
    expect(badge?.textContent).toBe("RGB");
    expect(badge?.title).toContain("RGB переведён в CMYK");
    expect(badge?.title).toContain("1, 2");
  });

  it("has no badge without conversion or after a failed conversion", async () => {
    const none = mount(base);
    const failed = mount({ ...base, color_conversion: { status: "failed", error: "x" } });
    await nextTick();
    expect(none.querySelector(".rgb-cmyk-indicator")).toBeNull();
    expect(failed.querySelector(".rgb-cmyk-indicator")).toBeNull();
  });
});
