import { afterEach, describe, expect, it, vi } from "vitest";
import { createApp, nextTick, reactive } from "vue";
import LowResPreview from "./LowResPreview.vue";
import OrderCard from "./OrderCard.vue";
import { LOW_RESOLUTION_CODE, lowResolutionGroup } from "./prepress.js";
import { api } from "../services/api.js";

const mounted = [];
function mountComponent(component, props) {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(component, props);
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return host;
}
const flush = async () => { for (let i = 0; i < 6; i += 1) { await Promise.resolve(); await nextTick(); } };
afterEach(() => { mounted.splice(0).forEach((unmount) => unmount()); vi.restoreAllMocks(); });

const lowIssue = { severity: "warning", action_id: 1150, message: "Low resolution image (118 ppi)" };
const lowGroup = { code: LOW_RESOLUTION_CODE, source: "pitstop", severity: "warning", message: "Низкое разрешение (PitStop)", count: 1, samples: [lowIssue.message] };
const result = {
  status: "ready", cached: false, pages: 2, marked_images: 4, images: null,
  legend: "Красные — ниже 250 ppi, зелёные — 250 ppi и выше. PitStop считает низким разрешение ниже 275 ppi.",
  image_url: "/api/checks/run-1/orders/42:1/lowres-preview/image?v=abc",
  pdf_url: "/api/checks/run-1/orders/42:1/lowres-preview/pdf?v=abc",
};
const card = (order) => mountComponent(OrderCard, { order: reactive({ order_id: "1", aggregate_id: "42:1", customer_id: "42", status: "warning", errors: [], files: [], ...order }), runId: "run-1", selected: false, reasons: [] });

describe("lowResolutionGroup", () => {
  it("finds a visible PitStop 1150 group only", () => {
    expect(lowResolutionGroup({ issue_groups: [lowGroup], pitstop: { issues: [lowIssue] } })).toEqual(lowGroup);
    expect(lowResolutionGroup({ issue_groups: [{ code: "action:2079", source: "pitstop", severity: "warning" }] })).toBeNull();
    expect(lowResolutionGroup({})).toBeNull();
  });

  it("ignores findings hidden by the fix baseline", () => {
    const hidden = { ...lowIssue, severity: "information", hidden_by_baseline: true, introduced_by_fix: 1, baseline_occurrences: 0, original_severity: "warning" };
    expect(lowResolutionGroup({ issue_groups: [{ ...lowGroup, severity: "info" }], pitstop: { issues: [hidden] } })).toBeNull();
  });
});

describe("OrderCard «Показать разрешение»", () => {
  it("is shown for every order with a PDF and recommended on visible action:1150", async () => {
    const plain = card({ pdf_path: "/p.pdf", issue_groups: [] });
    const low = card({ pdf_path: "/p.pdf", issue_groups: [lowGroup], pitstop: { issues: [lowIssue] } });
    const noPdf = card({ issue_groups: [lowGroup] });
    await nextTick();
    const plainButton = plain.querySelector(".lowres-preview-button");
    expect(plainButton?.textContent).toContain("Показать разрешение");
    expect(plainButton.disabled).toBe(false);
    expect(plainButton.classList.contains("recommended")).toBe(false);
    expect(plainButton.querySelector(".fix-recommended-dot")).toBeNull();
    const lowButton = low.querySelector(".lowres-preview-button");
    expect(lowButton.classList.contains("recommended")).toBe(true);
    expect(lowButton.querySelector(".fix-recommended-dot")).not.toBeNull();
    expect(noPdf.querySelector(".lowres-preview-button")).toBeNull();
  });

  it("is not recommended when the low-resolution finding was hidden by the baseline", async () => {
    const hidden = { ...lowIssue, severity: "information", hidden_by_baseline: true, introduced_by_fix: 1, baseline_occurrences: 0 };
    const host = card({ pdf_path: "/p.pdf", issue_groups: [{ ...lowGroup, severity: "info" }], pitstop: { issues: [hidden] } });
    await nextTick();
    expect(host.querySelector(".lowres-preview-button").classList.contains("recommended")).toBe(false);
  });

  it("builds the marking on click and shows legend, image and download links", async () => {
    let resolve;
    const call = vi.spyOn(api, "lowResPreview").mockReturnValue(new Promise((done) => { resolve = done; }));
    const host = card({ pdf_path: "/p.pdf" });
    await nextTick();
    host.querySelector(".lowres-preview-button").click();
    await nextTick();
    expect(call).toHaveBeenCalledWith("run-1", "42:1");
    expect(host.querySelector(".lowres-preview-button").textContent).toContain("Разметка…");
    expect(host.querySelector(".lowres-preview-button").disabled).toBe(true);
    resolve(result);
    await flush();
    expect(host.querySelector(".lowres-legend").textContent).toBe(result.legend);
    expect(host.querySelector(".lowres-count").textContent).toContain("Размечено изображений: 4");
    expect(host.querySelector(".lowres-image .preview-loader")).not.toBeNull();
    const links = [...host.querySelectorAll(".lowres-actions a")];
    expect(links.map((link) => link.textContent)).toEqual(["Скачать JPEG", "Скачать PDF с разметкой"]);
    expect(links[0].getAttribute("href")).toBe(result.image_url);
    expect(links[1].getAttribute("href")).toBe(result.pdf_url);
  });

  it("lists low images per page when the backend knows their ppi", async () => {
    vi.spyOn(api, "lowResPreview").mockResolvedValue({
      ...result,
      images: [{ page: 2, ppi: 173.4, low: true }, { page: 1, ppi: 118, low: true }, { page: 1, ppi: 292, low: false }, { page: 1, ppi: 118.2, low: true }],
    });
    const host = mountComponent(LowResPreview, { runId: "run-1", orderId: "42:1", order: { pdf_path: "/p.pdf" } });
    host.querySelector(".lowres-preview-button").click();
    await flush();
    expect([...host.querySelectorAll(".lowres-list li")].map((item) => item.textContent)).toEqual(["Стр. 1: 118 ppi, 118 ppi", "Стр. 2: 173 ppi"]);
  });

  it("shows a backend error inline", async () => {
    vi.spyOn(api, "lowResPreview").mockRejectedValue(new Error("В PDF нет цветных или полутоновых изображений: размечать нечего."));
    const host = card({ pdf_path: "/p.pdf" });
    await nextTick();
    host.querySelector(".lowres-preview-button").click();
    await flush();
    expect(host.querySelector(".lowres-preview .form-error").textContent).toContain("размечать нечего");
    expect(host.querySelector(".lowres-result")).toBeNull();
  });

  it("attaches to the return and shows the custom preview link", async () => {
    vi.spyOn(api, "lowResPreview").mockResolvedValue(result);
    const attach = vi.spyOn(api, "attachLowResPreview").mockResolvedValue({ filename: "1_return-preview.jpg", url: "/api/checks/run-1/orders/42:1/return-preview", custom_preview_url: "/api/checks/run-1/orders/42:1/return-preview" });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    const host = card({ pdf_path: "/p.pdf" });
    await nextTick();
    host.querySelector(".lowres-preview-button").click();
    await flush();
    host.querySelector(".lowres-attach").click();
    await flush();
    expect(confirm).not.toHaveBeenCalled();
    expect(attach).toHaveBeenCalledWith("run-1", "42:1");
    expect(host.querySelector(".lowres-attached")).not.toBeNull();
    expect(host.querySelector(".custom-preview a")?.textContent).toContain("Пользовательское превью выбрано для FTP");
  });

  it("asks before replacing an existing custom preview and keeps it on cancel", async () => {
    vi.spyOn(api, "lowResPreview").mockResolvedValue(result);
    const attach = vi.spyOn(api, "attachLowResPreview").mockResolvedValue({ url: "/x" });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const host = card({ pdf_path: "/p.pdf", custom_preview_url: "/api/checks/run-1/orders/42:1/return-preview" });
    await nextTick();
    host.querySelector(".lowres-preview-button").click();
    await flush();
    host.querySelector(".lowres-attach").click();
    await flush();
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(confirm.mock.calls[0][0]).toContain("Заменить");
    expect(attach).not.toHaveBeenCalled();
    confirm.mockReturnValue(true);
    host.querySelector(".lowres-attach").click();
    await flush();
    expect(attach).toHaveBeenCalledTimes(1);
  });
});
