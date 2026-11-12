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
