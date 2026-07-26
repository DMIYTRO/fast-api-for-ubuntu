import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useChecksStore } from "./checks.js";
import { api } from "../services/api.js";

describe("checks store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("filters and searches orders without embedding report data", () => {
    const store = useChecksStore();
    store.orders = [
      { order_id: "100", customer_id: "7", status: "passed", passed: true },
      { order_id: "200", customer_id: "8", status: "error", errors: ["RGB"] },
    ];
    store.setFilter("error");
    expect(store.filteredOrders.map((order) => order.order_id)).toEqual(["200"]);
    store.search = "100";
    expect(store.filteredOrders).toEqual([]);
  });

  it("updates only the order named by an SSE event", () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1", progress: 0 };
    store.orders = [{ order_id: "100", status: "processing" }, { order_id: "200", status: "processing" }];
    store.applyEvent({ type: "order.checked", run_id: "run-1", order_id: "100", status: "warning", processed: 1, total: 2 });
    expect(store.orders[0].status).toBe("warning");
    expect(store.orders[1].status).toBe("processing");
    expect(store.activeRun.progress).toBe(50);
  });

  it("makes previews from a completed order event visible immediately", () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1", progress: 0 };
    store.orders = [];
    store.applyEvent({
      type: "order.completed",
      run_id: "run-1",
      order_id: "100",
      order: {
        order_id: "100",
        status: "completed",
        preview_paths: ["/input/Previews/job-face_preview.png"],
        files: [{
          name: "job-face.jpg",
          parsed: { side: "face" },
        }],
      },
    });

    expect(store.orders[0].files[0]).toMatchObject({
      id: "run-1:100:0",
      filename: "job-face.jpg",
      side: "face",
      preview_url: "/api/files/run-1%3A100%3A0/preview",
    });
  });

  it("does not merge preview artifact paths into an order", () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    store.orders = [{ order_id: "100", status: "processing" }];

    store.applyEvent({
      type: "preview.created",
      run_id: "run-1",
      order_id: "100",
      path: "/input/Previews/job-face_preview.png",
    });

    expect(store.orders[0]).toEqual({ order_id: "100", status: "processing" });
  });

  it("permits print only for a non-empty passed selection", () => {
    const store = useChecksStore();
    store.orders = [{ order_id: "100", status: "passed", passed: true }, { order_id: "200", status: "error" }];
    store.selected = ["100"];
    expect(store.canPrint).toBe(true);
    store.selected.push("200");
    expect(store.canPrint).toBe(false);
  });

  it("selects and clears all orders visible through the current filter", () => {
    const store = useChecksStore();
    store.orders = [
      { order_id: "100", status: "passed", passed: true },
      { order_id: "200", status: "error", errors: ["RGB"] },
      { order_id: "300", status: "passed", passed: true },
    ];
    store.setFilter("passed");

    store.toggleAllFiltered();
    expect(store.selected).toEqual(["100", "300"]);

    store.toggleAllFiltered();
    expect(store.selected).toEqual([]);
  });

  it("sends each selected order its own return reasons", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1", progress: 73 };
    store.orders = [{ order_id: "100" }, { order_id: "200" }];
    store.selected = ["100", "200"];
    store.returnComments = { "100": "Размер неверный.", "200": "Низкое разрешение." };
    const reject = vi.spyOn(api, "prepareReject").mockImplementation(async ({ order_ids }) => ({
      items: [{ order_id: order_ids[0], status: "prepared" }],
    }));
    const getRun = vi.spyOn(api, "run");
    vi.spyOn(api, "orders").mockResolvedValue({ items: store.orders });

    await store.act("reject", "Общий комментарий.");

    expect(reject).toHaveBeenNthCalledWith(1, {
      order_ids: ["100"], run_id: "run-1", comment: "Размер неверный.\nОбщий комментарий.",
    });
    expect(reject).toHaveBeenNthCalledWith(2, {
      order_ids: ["200"], run_id: "run-1", comment: "Низкое разрешение.\nОбщий комментарий.",
    });
    expect(store.returnComments).toEqual({});
    expect(store.activeRun.progress).toBe(73);
    expect(getRun).not.toHaveBeenCalled();
  });
});
