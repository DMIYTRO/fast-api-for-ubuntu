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
});
