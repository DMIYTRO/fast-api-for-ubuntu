import { beforeEach, describe, expect, it, vi } from "vitest";
import { createPinia, setActivePinia } from "pinia";
import { useChecksStore, isOrderPrintable, isOrderForcePrintable, needsForcedBuild } from "./checks.js";
import { orderIdentity } from "./orderIdentity.js";
import { peekReturnDraft, returnDraft } from "./returnDrafts.js";
import { api } from "../services/api.js";
import * as events from "../services/events.js";

describe("checks store", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("ignores an older selection that resolves after the latest run", async () => {
    const store = useChecksStore();
    let resolveOld;
    vi.spyOn(api, "run")
      .mockReturnValueOnce(new Promise((resolve) => { resolveOld = resolve; }))
      .mockResolvedValueOnce({ id: "new", status: "running" });
    vi.spyOn(api, "orders").mockResolvedValue({ items: [{ order_id: "new-order" }] });
    const connect = vi.spyOn(events, "connectRunEvents").mockReturnValue(vi.fn());

    const oldSelection = store.selectRun("old");
    await store.selectRun("new");
    resolveOld({ id: "old", status: "running" });
    await oldSelection;

    expect(store.activeRun.id).toBe("new");
    expect(store.orders[0].order_id).toBe("new-order");
    expect(connect).toHaveBeenCalledExactlyOnceWith("new", expect.any(Object));
  });

  it("keeps a delayed page and obsolete stream from changing the selected run", async () => {
    const store = useChecksStore();
    let resolveOrders;
    let resolveResync;
    vi.spyOn(api, "run")
      .mockResolvedValueOnce({ id: "old", status: "running" })
      .mockReturnValueOnce(new Promise((resolve) => { resolveResync = resolve; }))
      .mockResolvedValueOnce({ id: "new", status: "running" });
    vi.spyOn(api, "orders")
      .mockResolvedValueOnce({ items: [] })
      .mockReturnValueOnce(new Promise((resolve) => { resolveOrders = resolve; }))
      .mockResolvedValueOnce({ items: [{ order_id: "new-order" }] });
    const oldDisconnect = vi.fn();
    const newDisconnect = vi.fn();
    const connect = vi.spyOn(events, "connectRunEvents")
      .mockReturnValueOnce(oldDisconnect)
      .mockReturnValueOnce(newDisconnect);

    await store.selectRun("old");
    const oldPage = store.loadOrders(1);
    const oldStream = connect.mock.calls[0][1];
    const oldResync = oldStream.onState("reconnecting");
    await store.selectRun("new");
    await connect.mock.calls[1][1].onState("connected");
    resolveOrders({ items: [{ order_id: "old-order" }] });
    resolveResync({ id: "old", status: "completed" });
    await Promise.all([oldPage, oldResync]);
    await oldStream.onState("closed");
    oldStream.onEvent({ type: "run.completed" });

    expect(store.activeRun).toEqual({ id: "new", status: "running" });
    expect(store.orders[0].order_id).toBe("new-order");
    expect(store.events).toEqual([]);
    expect(store.connection).toBe("connected");
    expect(oldDisconnect).toHaveBeenCalledOnce();
    expect(newDisconnect).not.toHaveBeenCalled();
  });

  it("does not connect after leaving the dashboard while a selection is pending", async () => {
    const store = useChecksStore();
    let resolveRun;
    vi.spyOn(api, "run").mockReturnValue(new Promise((resolve) => { resolveRun = resolve; }));
    const connect = vi.spyOn(events, "connectRunEvents");

    const selection = store.selectRun("run-1");
    store.stopListening();
    resolveRun({ id: "run-1", status: "running" });
    await selection;

    expect(connect).not.toHaveBeenCalled();
    expect(store.connection).toBe("closed");
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
    expect(store.filteredOrders.map((order) => order.order_id)).toEqual(["200"]);
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

  it("reloads the run and the orders page after an accepted Callas fix", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1", status: "completed" };
    store.orders = [{ order_id: "101", status: "warning", current_pdf_revision: 2 }];
    const run = vi.spyOn(api, "run").mockResolvedValue({ id: "run-1", status: "completed", progress: 100 });
    vi.spyOn(api, "orders").mockResolvedValue({
      items: [{ order_id: "101", status: "passed", current_pdf_revision: 3 }],
      page: 1, page_size: 10, total: 1, total_pages: 1,
      counts: { all: 1, passed: 1, warning: 0, error: 0, waiting_confirmation: 0 },
    });

    await store.refreshAfterFix();

    expect(run).toHaveBeenCalledWith("run-1", { include_orders: false });
    expect(store.orders.map((order) => [order.status, order.current_pdf_revision])).toEqual([["passed", 3]]);
  });

  it("still reloads the orders when the run refresh fails", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    vi.spyOn(api, "run").mockRejectedValue(new Error("boom"));
    const orders = vi.spyOn(api, "orders").mockResolvedValue({ items: [], page: 1, total: 0, total_pages: 1, counts: {} });

    await store.refreshAfterFix();

    expect(orders).toHaveBeenCalledTimes(1);
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

  it("allows yellow orders and blocks red ones unless the server marks a final PDF as forced-printable", () => {
    const store = useChecksStore();
    const forced = {
      order_id: "400", status: "error", print_mode: "forced", current_pdf_revision: 1,
      pitstop: { execution_status: "completed", verdict: "passed", checked_revision: 1 },
      print_blockers: [{ kind: "confirm", code: "forced_print_confirmation", message: "Заказ с ошибками." }],
    };
    store.orders = [
      { order_id: "100", status: "passed" },
      { order_id: "200", status: "error" },
      { order_id: "300", status: "warning" },
      forced,
    ];
    const pick = (...ids) => { store.selected = ids.map((id) => orderIdentity(store.orders.find((order) => order.order_id === id))); };
    pick("100", "300");
    expect(store.canPrint).toBe(true);
    expect(store.printNeedsConfirmation).toBe(false);
    pick("200");
    expect(store.canPrint).toBe(false);
    pick("100", "200");
    expect(store.canPrint).toBe(false);
    pick("100", "400");
    expect(store.canPrint).toBe(true);
    expect(store.printNeedsConfirmation).toBe(true);
    expect(isOrderPrintable(forced)).toBe(false);
    expect(isOrderForcePrintable(forced)).toBe(true);
    expect(isOrderForcePrintable({ ...forced, print_mode: "blocked" })).toBe(false);
    expect(isOrderForcePrintable({ ...forced, pitstop: { ...forced.pitstop, execution_status: "failed" } })).toBe(false);
    expect(isOrderForcePrintable({ ...forced, current_pdf_revision: 2 })).toBe(false);
  });

  it("offers a forced build for a red low-DPI order regardless of the diagnostic PitStop", () => {
    const store = useChecksStore();
    const message = "Заказ с ошибками (низкое разрешение: от 197 DPI при норме 250). Будет собран итоговый PDF и отправлен в печать под вашу ответственность.";
    const build = {
      order_id: "26252239", status: "error", print_mode: "forced", pdf_kind: "diagnostic", current_pdf_revision: 1,
      pitstop: { execution_status: "failed", verdict: "error", checked_revision: 1 },
      print_blockers: [{ kind: "confirm", code: "forced_print_build", message }],
    };
    store.orders = [{ order_id: "100", status: "passed" }, build];
    store.selected = [orderIdentity(build)];
    expect(isOrderForcePrintable(build)).toBe(true);
    expect(needsForcedBuild(build)).toBe(true);
    expect(store.canPrint).toBe(true);
    expect(store.printNeedsConfirmation).toBe(true);
    expect(store.printNeedsBuild).toBe(true);
    expect(store.printConfirmMessages).toEqual([message]);
    store.selected = store.orders.map(orderIdentity);
    expect(store.printConfirmMessages).toEqual([`26252239: ${message}`]);
    // Сборка возможна только для режима forced без других блокеров.
    expect(isOrderForcePrintable({ ...build, print_mode: "blocked", print_blockers: [...build.print_blockers, { kind: "approval", code: "callas_fix_pending", message: "" }] })).toBe(false);
    store.selected = [orderIdentity(store.orders[0])];
    expect(store.printNeedsBuild).toBe(false);
  });

  it("requires a clean completed PitStop result tied to the current revision", () => {
    const order = { status: "passed", current_pdf_revision: 2, current_pdf_sha256: "new", pitstop: {
      execution_status: "completed", verdict: "passed", checked_revision: 2, input_sha256: "new",
    } };
    expect(isOrderPrintable(order)).toBe(true);
    // Вердикт PitStop не блокирует печать: он уже учтён в цвете заказа.
    expect(isOrderPrintable({ ...order, pitstop: { ...order.pitstop, verdict: "warning" } })).toBe(true);
    expect(isOrderPrintable({ ...order, status: "warning", pitstop: { ...order.pitstop, verdict: "error" } })).toBe(true);
    for (const patch of [{ execution_status: "failed" }, { checked_revision: 1 }, { input_sha256: "old" }]) {
      expect(isOrderPrintable({ ...order, pitstop: { ...order.pitstop, ...patch } })).toBe(false);
    }
    // Старые записи без хэша в ответе PitStop не должны навсегда блокировать печать зелёного заказа.
    const { input_sha256: _omitted, ...legacyPitstop } = order.pitstop;
    expect(isOrderPrintable({ ...order, pitstop: legacyPitstop })).toBe(true);
    expect(isOrderPrintable({ status: "passed", pitstop_required: true })).toBe(false);
    expect(isOrderPrintable({ status: "completed", pitstop_status: "technical_error" })).toBe(false);
    expect(isOrderPrintable({ status: "completed", pitstop_status: "not_checked" })).toBe(true);
  });

  it("blocks print with the server-provided reason (file, check or approval)", () => {
    const blocked = { status: "passed", print_blockers: [{ kind: "approval", code: "callas_fix_pending", message: "Исправление Callas ждёт решения." }] };
    expect(isOrderPrintable(blocked)).toBe(false);
    expect(isOrderPrintable({ status: "passed", print_blockers: [] })).toBe(true);
    const store = useChecksStore();
    store.orders = [{ ...blocked, order_id: "9" }];
    store.selected = [orderIdentity(store.orders[0])];
    expect(store.canPrint).toBe(false);
    expect(store.printBlockReason).toBe("Исправление Callas ждёт решения.");
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
      order_ids: ["100"], run_id: "run-1", comment: "Размер неверный.\nОбщий комментарий.", design: false, design_cost: "0", conflict_strategy: "replace",
    });
    expect(reject).toHaveBeenNthCalledWith(2, {
      order_ids: ["200"], run_id: "run-1", comment: "Низкое разрешение.\nОбщий комментарий.", design: true, design_cost: "50", conflict_strategy: "replace",
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

  it("uses pre-selected check suggestions for orders whose card is not mounted", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    const suggestion = { text: "Низкое разрешение изображений", category: "Разрешение", confidence: "high", preselect: true, evidence: "PitStop 1150: 197 ppi" };
    store.orders = [
      { order_id: "300", status: "error", return_suggestions: [suggestion, { ...suggestion, text: "Макет в RGB", preselect: false }] },
      { order_id: "400", status: "error", return_suggestions: [suggestion] },
    ];
    store.selected = store.orders.map(orderIdentity);
    // Оператор снял все предложения у второго заказа: предложение не возвращается.
    returnDraft(orderIdentity(store.orders[1])).touched = true;
    expect(store.selectedReturnComments).toEqual({ [orderIdentity(store.orders[0])]: "Низкое разрешение изображений." });
    const reject = vi.spyOn(api, "prepareReject").mockImplementation(async ({ order_ids }) => ({
      items: [{ order_id: order_ids[0], status: "prepared" }],
    }));
    vi.spyOn(api, "orders").mockResolvedValue({ items: store.orders });

    await store.act("reject", "");

    expect(reject.mock.calls.map(([body]) => body.comment)).toEqual(["Низкое разрешение изображений.", ""]);
    expect(peekReturnDraft(orderIdentity(store.orders[1]))).toBeUndefined();
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
      order_ids: ["client-a:100", "client-b:100"], run_id: "run-1", conflict_strategy: "replace", confirm_failed_processing: false,
    });
  });

  it("prints a yellow order directly", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    store.orders = [{ order_id: "100", status: "warning" }];
    store.selected = store.orders.map(orderIdentity);
    const print = vi.spyOn(api, "preparePrint").mockResolvedValue({ items: [] });
    vi.spyOn(api, "orders").mockResolvedValue({ items: [] });

    await store.act("print", "");

    expect(print).toHaveBeenCalledWith(expect.objectContaining({ order_ids: ["100"], confirm_failed_processing: false }));
  });

  it("rejects direct print of a red order without a ready final PDF and the legacy override", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    store.orders = [{ order_id: "100", status: "error", print_mode: "blocked", print_blockers: [{ kind: "file", code: "no_final_pdf", message: "Готового итогового PDF нет." }] }];
    store.selected = store.orders.map(orderIdentity);
    const print = vi.spyOn(api, "preparePrint");
    const reject = vi.spyOn(api, "prepareReject");

    await expect(store.act("print", "", "fail", true)).rejects.toThrow("Готового итогового PDF нет");
    await expect(store.act("force-print", "")).rejects.toThrow();

    expect(print).not.toHaveBeenCalled();
    expect(reject).not.toHaveBeenCalled();
    expect(store.orders[0].status).toBe("error");
  });

  it("sends a red order with a final PDF only after the operator confirms", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    store.orders = [{
      order_id: "100", status: "error", print_mode: "forced", current_pdf_revision: 1,
      pitstop: { execution_status: "completed", verdict: "passed", checked_revision: 1 },
      print_blockers: [{ kind: "confirm", code: "forced_print_confirmation", message: "Заказ с ошибками." }],
    }];
    store.selected = store.orders.map(orderIdentity);
    const print = vi.spyOn(api, "preparePrint").mockResolvedValue({ items: [] });
    vi.spyOn(api, "orders").mockResolvedValue({ items: [] });

    await expect(store.act("print", "")).rejects.toThrow("подтверждения оператора");
    expect(print).not.toHaveBeenCalled();

    await store.act("print", "", "fail", true);

    expect(print).toHaveBeenCalledWith({
      order_ids: ["100"], run_id: "run-1", conflict_strategy: "fail", confirm_failed_processing: true,
    });
  });
});

describe("rejectGroups", () => {
  it("keeps kit orders together with the main order first", async () => {
    const { rejectGroups } = await import("./checks.js");
    const kit = (position) => ({ main_id: "26234881", position, size: 3 });
    const groups = rejectGroups([
      { order_id: "26234883", kit: kit(3) },
      { order_id: "26234424" },
      { order_id: "26234881", kit: kit(1) },
      { order_id: "26234882", kit: kit(2) },
    ]);
    expect(groups.map((group) => group.map((order) => order.order_id))).toEqual([
      ["26234881", "26234882", "26234883"],
      ["26234424"],
    ]);
  });
});

describe("run kits", () => {
  beforeEach(() => {
    setActivePinia(createPinia());
    localStorage.clear();
    vi.restoreAllMocks();
  });

  it("annotates orders with kits from the API and lists selected kits", async () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1" };
    store.orders = [{ order_id: "26234883" }, { order_id: "26234424" }];
    vi.spyOn(api, "runKits").mockResolvedValue({
      kits: { 26234883: { main_id: "26234881", member_ids: [], position: 3, size: 19 } },
    });

    await store.loadKits("run-1");
    store.toggle(store.orders[0]);
    store.toggle(store.orders[1]);

    expect(store.orders[0].kit.main_id).toBe("26234881");
    expect(store.orders[1].kit).toBeUndefined();
    expect(store.selectedKits).toEqual([{ main_id: "26234881", size: 19, selected: 1 }]);
    await store.loadKits("run-1");
    expect(api.runKits).toHaveBeenCalledTimes(1); // повтор не чаще раза в 20 с
  });

  it("adds a late customer comment to an already shown card, keeping its other fields", () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1", progress: 0 };
    store.orders = [
      { aggregate_id: "42:200", customer_id: "42", order_id: "200", status: "waiting_confirmation", pending_confirmations: 1, files: [{ filename: "a.jpg" }] },
    ];
    const comment = "друк з лицьової сторони\nдругий рядок";

    store.applyEvent({
      type: "order.customer_comment", run_id: "run-1", aggregate_id: "42:200", customer_id: "42", order_id: "200",
      status: "waiting_confirmation",
      order: { aggregate_id: "42:200", customer_id: "42", order_id: "200", status: "waiting_confirmation", pending_confirmations: 1, customer_comment: comment, files: [{ filename: "a.jpg" }] },
    });

    expect(store.orders).toHaveLength(1);
    expect(store.orders[0].customer_comment).toBe(comment);
    expect(store.orders[0].status).toBe("waiting_confirmation");
    expect(store.orders[0].pending_confirmations).toBe(1);
  });

  it("does not lose the comment when a later status-only event arrives", () => {
    const store = useChecksStore();
    store.activeRun = { id: "run-1", progress: 0 };
    store.orders = [{ aggregate_id: "42:200", customer_id: "42", order_id: "200", status: "processing", customer_comment: "текст", files: [] }];

    store.applyEvent({ type: "order.checked", run_id: "run-1", aggregate_id: "42:200", customer_id: "42", order_id: "200", status: "warning" });

    expect(store.orders[0].customer_comment).toBe("текст");
    expect(store.orders[0].status).toBe("warning");
  });
});
