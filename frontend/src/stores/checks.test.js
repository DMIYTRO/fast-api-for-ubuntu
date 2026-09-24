import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useChecksStore } from "./checks.js";
import { orderIdentity } from "./orderIdentity.js";
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

  it("uses only the final status for filters and ignores legacy passed and issue arrays", () => {
    const store = useChecksStore();
    store.orders = [
      { order_id: "100", status: "error", passed: true },
      { order_id: "200", status: "passed", errors: ["Старый результат"] },
      { order_id: "300", status: "warning" },
    ];

    store.setFilter("passed");
    expect(store.filteredOrders.map((order) => order.order_id)).toEqual(["200", "300"]);
    store.setFilter("error");
    expect(store.filteredOrders.map((order) => order.order_id)).toEqual(["100"]);
  });

  it("loads requested order pages and retains selected orders across pages", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    vi.spyOn(api, "orders")
      .mockResolvedValueOnce({
        items: [{ order_id: "101", status: "passed" }],
        page: 1, page_size: 10, total: 21, total_pages: 3,
        counts: { all: 21, passed: 15, warning: 3, error: 3, waiting_confirmation: 0 },
      })
      .mockResolvedValueOnce({
        items: [{ order_id: "111", status: "passed" }],
        page: 2, page_size: 10, total: 21, total_pages: 3,
        counts: { all: 21, passed: 15, warning: 3, error: 3, waiting_confirmation: 0 },
      });

    await store.loadOrders(1);
    store.toggleAllFiltered();
    await store.setPage(2);

    expect(api.orders).toHaveBeenNthCalledWith(1, "run-1", {
      page: 1, page_size: 10, status: "passed", search: "", active_only: true,
    });
    expect(store.pageInfo.total_pages).toBe(3);
    expect(store.orders.map((order) => order.order_id)).toEqual(["111"]);
    expect(store.selectedOrders.map((order) => order.order_id)).toEqual(["101"]);
    store.applyEvent({
      type: "order.pitstop_completed", run_id: "run-1",
      order: { order_id: "101", status: "error" },
    });
    expect(store.selected).toEqual([]);
    expect(store.canPrint).toBe(false);
  });

  it("clamps the requested page when the result set shrinks", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    vi.spyOn(api, "orders")
      .mockResolvedValueOnce({ items: [], page: 5, page_size: 10, total: 12, total_pages: 2, counts: {} })
      .mockResolvedValueOnce({ items: [{ order_id: "112", status: "passed" }], page: 2, page_size: 10, total: 12, total_pages: 2, counts: {} });

    await store.loadOrders(5);

    expect(api.orders).toHaveBeenNthCalledWith(2, "run-1", {
      page: 2, page_size: 10, status: "passed", search: "", active_only: true,
    });
    expect(store.page).toBe(2);
    expect(store.orders[0].order_id).toBe("112");
  });

  it("restores the latest completed run and its previews after a page refresh", async () => {
    const store = useChecksStore();
    vi.spyOn(api, "config").mockResolvedValue({});
    vi.spyOn(api, "runs").mockResolvedValue({ items: [{ id: "done", status: "completed" }] });
    vi.spyOn(api, "run").mockResolvedValue({ id: "done", status: "completed" });
    vi.spyOn(api, "orders").mockResolvedValue({
      items: [{ order_id: "123", status: "passed", files: [{ filename: "art.pdf", preview_path: "/mnt/share/Previews/art_preview.png" }] }],
      page: 1, page_size: 10, total: 1, total_pages: 1, counts: { all: 1, passed: 1 },
    });

    await store.initialize();

    expect(store.activeRun.id).toBe("done");
    expect(store.orders[0].files[0].preview_url).toBe("/api/files/done%3A123%3A0/preview");
  });

  it("keeps duplicate order numbers separated by customer across cards, selection, and events", () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1", progress: 0 };
    store.orders = [
      { aggregate_id: "client-a:100", customer_id: "client-a", order_id: "100", status: "processing", files: [{ filename: "a.pdf" }] },
      { aggregate_id: "client-b:100", customer_id: "client-b", order_id: "100", status: "processing", files: [{ filename: "b.pdf" }] },
    ];

    store.toggle(store.orders[0]);
    store.toggle(store.orders[1]);
    expect(store.selected).toEqual(["aggregate:client-a:100", "aggregate:client-b:100"]);
    expect(store.selectedOrders.map((order) => order.customer_id)).toEqual(["client-a", "client-b"]);
    store.applyEvent({
      type: "order.updated", run_id: "run-1",
      order: { ...store.orders[0], files: [{ filename: "a.pdf" }] },
    });
    expect(store.orders[0].files[0].id).toBe("run-1:client-a:100:0");

    store.applyEvent({
      type: "order.checked", run_id: "run-1",
      order: { aggregate_id: "client-b:100", customer_id: "client-b", order_id: "100", status: "warning" },
    });
    expect(store.orders.map((order) => order.status)).toEqual(["processing", "warning"]);
    expect(store.selectedOrderSnapshots["aggregate:client-b:100"].customer_id).toBe("client-b");

    store.applyEvent({
      type: "order.checked", run_id: "run-1", aggregate_id: "client-a:100",
      customer_id: "client-a", order_id: "100", status: "warning",
    });
    expect(store.orders.map((order) => order.status)).toEqual(["warning", "warning"]);
  });

  it("keeps the loaded first card in place when a new order arrives over SSE", () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-stable" };
    store.pageSize = 10;
    store.orders = [
      { aggregate_id: "customer-z:900", customer_id: "customer-z", order_id: "900", status: "passed" },
    ];

    store.applyEvent({
      type: "order.completed", run_id: "run-stable",
      order: { aggregate_id: "customer-a:100", customer_id: "customer-a", order_id: "100", status: "passed" },
    });

    expect(store.orders.map((order) => order.order_id)).toEqual(["900", "100"]);
  });

  it("does not let an orders response started before a newer SSE event overwrite that event", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-race" };
    let resolveOrders;
    vi.spyOn(api, "orders").mockReturnValue(new Promise((resolve) => { resolveOrders = resolve; }));

    const request = store.loadOrders(1);
    store.applyEvent({
      type: "order.completed", run_id: "run-race",
      order: { aggregate_id: "client-a:100", customer_id: "client-a", order_id: "100", status: "completed" },
    });
    resolveOrders({
      items: [{ aggregate_id: "client-a:100", customer_id: "client-a", order_id: "100", status: "processing" }],
      page: 1, page_size: 10, total: 1, total_pages: 1, counts: {},
    });
    await request;

    expect(store.orders).toHaveLength(1);
    expect(store.orders[0].status).toBe("completed");
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

  it("targets correction by aggregate identity when order numbers collide", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    const correction = vi.spyOn(api, "correction").mockResolvedValue({});
    vi.spyOn(api, "run").mockResolvedValue({ id: "run-1", status: "running" });
    vi.spyOn(api, "orders").mockResolvedValue({ items: [] });

    await store.decide({ aggregate_id: "client-b:100", customer_id: "client-b", order_id: "100" }, "approve");

    expect(correction).toHaveBeenCalledWith("run-1", "client-b:100", { decision: "approve" });
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
    store.selected = [orderIdentity(store.orders.find((order) => order.order_id === "100"))];
    expect(store.canPrint).toBe(true);
    store.selected.push("200");
    expect(store.canPrint).toBe(false);
  });

  it("permits confirmed print only when every selected order is passed or has an error", () => {
    const store = useChecksStore();
    store.orders = [
      { order_id: "100", status: "passed" },
      { order_id: "200", status: "error" },
      { order_id: "300", status: "processing" },
    ];
    store.selected = store.orders.slice(0, 2).map(orderIdentity);
    expect(store.canForcePrint).toBe(true);

    store.selected.push(orderIdentity(store.orders[2]));
    expect(store.canForcePrint).toBe(false);
  });

  it("blocks print while PitStop is running or after a technical failure", () => {
    const store = useChecksStore();
    store.orders = [{
      order_id: "100",
      status: "passed",
      pitstop: { execution_status: "running" },
    }];
    store.selected = [orderIdentity(store.orders.find((order) => order.order_id === "100"))];
    expect(store.canPrint).toBe(false);

    store.orders[0].pitstop = { execution_status: "failed", verdict: "unknown" };
    expect(store.canPrint).toBe(false);

    store.orders[0].pitstop = { execution_status: "completed", verdict: "passed" };
    expect(store.canPrint).toBe(true);
  });

  it("removes a selected printable order when a PitStop event makes it invalid without moving the card", () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    store.orders = [
      { order_id: "100", status: "passed" },
      { order_id: "200", status: "passed" },
      { order_id: "300", status: "passed" },
    ];
    store.selected = [orderIdentity(store.orders.find((order) => order.order_id === "200"))];

    store.applyEvent({
      type: "order.pitstop_completed",
      run_id: "run-1",
      order: {
        order_id: "200",
        status: "error",
        pitstop: { execution_status: "completed", verdict: "error" },
      },
    });

    expect(store.orders.map((order) => order.order_id)).toEqual(["100", "200", "300"]);
    expect(store.orders[1].pitstop.verdict).toBe("error");
    expect(store.selected).toEqual([]);
    expect(store.canPrint).toBe(false);
  });

  it("removes a selected order when PitStop starts rechecking an otherwise passed PDF", () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    store.orders = [{ order_id: "100", status: "passed" }];
    store.selected = [orderIdentity(store.orders.find((order) => order.order_id === "100"))];

    store.applyEvent({
      type: "order.pitstop_started",
      run_id: "run-1",
      order: {
        order_id: "100",
        status: "pitstop_checking",
        pitstop: { execution_status: "running" },
      },
    });

    expect(store.selected).toEqual([]);
  });

  it("hides orders already accepted for print from the passed filter", () => {
    const store = useChecksStore();
    store.orders = [
      { order_id: "100", status: "passed", passed: true },
      { order_id: "200", status: "accepted_for_print", passed: true },
    ];
    store.setFilter("passed");

    expect(store.filteredOrders.map((order) => order.order_id)).toEqual(["100"]);
  });

  it("hides completed workflow orders from the all-orders work queue", () => {
    const store = useChecksStore();
    store.orders = [
      { order_id: "100", status: "passed", passed: true },
      { order_id: "200", status: "accepted_for_print", passed: true },
      { order_id: "300", status: "returned_for_rework" },
    ];

    expect(store.filteredOrders.map((order) => order.order_id)).toEqual(["100"]);
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
    expect(store.selected).toEqual([orderIdentity(store.orders[0]), orderIdentity(store.orders[2])]);

    store.toggleAllFiltered();
    expect(store.selected).toEqual([]);
  });

  it("sends each selected order its own return reasons", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1", progress: 73 };
    store.orders = [{ order_id: "100" }, { order_id: "200" }];
    store.selected = store.orders.map(orderIdentity);
    store.returnComments = { [orderIdentity(store.orders[0])]: "Размер неверный.", [orderIdentity(store.orders[1])]: "Низкое разрешение." };
    store.setReturnDesign(store.orders[0], false);
    store.setReturnDesignCost(store.orders[1], "50");
    const reject = vi.spyOn(api, "prepareReject").mockImplementation(async ({ order_ids }) => ({
      items: [{ order_id: order_ids[0], status: "prepared" }],
    }));
    const getRun = vi.spyOn(api, "run");
    vi.spyOn(api, "orders").mockResolvedValue({ items: store.orders });

    await store.act("reject", "Общий комментарий.");

    expect(reject).toHaveBeenNthCalledWith(1, {
      order_ids: ["100"], run_id: "run-1", comment: "Размер неверный.\nОбщий комментарий.", design: false, design_cost: "0", conflict_strategy: "fail",
    });
    expect(reject).toHaveBeenNthCalledWith(2, {
      order_ids: ["200"], run_id: "run-1", comment: "Низкое разрешение.\nОбщий комментарий.", design: true, design_cost: "50", conflict_strategy: "fail",
    });
    expect(store.returnComments).toEqual({});
    expect(store.activeRun.progress).toBe(73);
    expect(getRun).not.toHaveBeenCalled();
  });

  it("removes successfully returned orders from the active queue even after a stale refresh", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    store.orders = [{ order_id: "100", status: "error" }];
    store.selected = [orderIdentity(store.orders.find((order) => order.order_id === "100"))];
    vi.spyOn(api, "prepareReject").mockResolvedValue({
      items: [{ order_id: "100", status: "prepared" }],
    });
    // A delayed persistence read may still have the old status, but the
    // successful action response must keep this order out of the work queue.
    vi.spyOn(api, "orders").mockResolvedValue({
      items: [{ order_id: "100", status: "error" }],
    });

    await store.act("reject", "Исправить макет.");

    expect(store.orders[0].status).toBe("returned_for_rework");
    expect(store.filteredOrders).toEqual([]);
    expect(store.selected).toEqual([]);
  });

  it("keeps failed rework orders selected so they can be retried", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    store.orders = [{ order_id: "100" }, { order_id: "200" }];
    store.selected = store.orders.map(orderIdentity);
    store.returnComments = { [orderIdentity(store.orders[0])]: "Причина 1", [orderIdentity(store.orders[1])]: "Причина 2" };
    vi.spyOn(api, "prepareReject").mockImplementation(async ({ order_ids }) => ({
      items: [{
        order_id: order_ids[0],
        status: order_ids[0] === "100" ? "prepared" : "error",
        ...(order_ids[0] === "200" ? { message: "Sborka не отвечает." } : {}),
      }],
    }));
    vi.spyOn(api, "orders").mockResolvedValue({ items: store.orders });

    const result = await store.act("reject", "");

    expect(result.map((item) => item.status)).toEqual(["prepared", "error"]);
    expect(store.selected).toEqual([orderIdentity(store.orders[1])]);
    expect(store.returnComments).toEqual({ [orderIdentity(store.orders[1])]: "Причина 2" });
  });

  it("uses aggregate IDs in workflow actions when customer order numbers collide", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    store.orders = [
      { aggregate_id: "client-a:100", customer_id: "client-a", order_id: "100", status: "passed" },
      { aggregate_id: "client-b:100", customer_id: "client-b", order_id: "100", status: "passed" },
    ];
    store.selected = store.orders.map(orderIdentity);
    const print = vi.spyOn(api, "preparePrint").mockResolvedValue({ items: [] });
    vi.spyOn(api, "orders").mockResolvedValue({ items: [] });

    await store.act("print", "");

    expect(print).toHaveBeenCalledWith({
      order_ids: ["client-a:100", "client-b:100"], run_id: "run-1", conflict_strategy: "fail", confirm_failed_processing: false,
    });
  });

  it("sends explicit confirmation when printing an order with an error", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    store.orders = [{ order_id: "100", status: "error" }];
    store.selected = [orderIdentity(store.orders.find((order) => order.order_id === "100"))];
    const print = vi.spyOn(api, "preparePrint").mockResolvedValue({
      items: [{ order_id: "100", status: "prepared" }],
    });
    vi.spyOn(api, "orders").mockResolvedValue({ items: store.orders });

    await store.act("force-print", "");

    expect(print).toHaveBeenCalledWith({
      order_ids: ["100"], run_id: "run-1", conflict_strategy: "fail", confirm_failed_processing: true,
    });
    expect(store.orders[0].status).toBe("accepted_for_print");
  });
});
