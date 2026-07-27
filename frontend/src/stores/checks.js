import { defineStore } from "pinia";
import { api } from "../services/api.js";
import { connectRunEvents } from "../services/events.js";

const list = (data) => Array.isArray(data) ? data : (data?.items || data?.orders || []);
const running = (status) => ["queued", "running", "waiting_confirmation", "cancelling"].includes(status);
const decorateOrder = (order, runId) => {
  if (!order) return order;
  const orderId = String(order.order_id ?? order.id ?? "");
  const previews = order.preview_paths || [];
  const files = (order.files || order.file_results || []).map((file, index) => {
    const parsed = file.parsed || {};
    const side = file.side || parsed.side;
    const id = String(file.id || `${runId}:${orderId}:${index}`);
    const matchingPreview = file.preview_path || previews.find((path) =>
      side && String(path).toLowerCase().includes(String(side).toLowerCase())
    );
    return {
      ...file,
      id,
      filename: file.filename || file.name,
      side,
      preview_url: file.preview_url || (matchingPreview
        ? `/api/files/${encodeURIComponent(id)}/preview`
        : ""),
    };
  });
  return { ...order, files };
};
const decorateOrders = (data, runId) => list(data).map((order) => decorateOrder(order, runId));

export const useChecksStore = defineStore("checks", {
  state: () => ({
    runs: [], activeRun: null, orders: [], config: null, loading: false, error: "",
    connection: "closed", selected: [], filter: localStorage.getItem("im-filter") || "all",
    search: "", events: [], drawerOpen: false, stopEvents: null, actionResults: {}, returnComments: {},
  }),
  getters: {
    filteredOrders(state) {
      const query = state.search.trim().toLowerCase();
      return state.orders.filter((order) => {
        const status = order.status || (order.passed ? "passed" : "error");
        // This dashboard is an active work queue.  Orders already handed to
        // print or returned for rework belong to their file folders/history,
        // not to any of the active tabs (including "Все").
        if (["accepted_for_print", "returned_for_rework"].includes(status)) return false;
        const filterOk = state.filter === "all" || status === state.filter ||
          (state.filter === "passed" && ["passed", "warning", "completed"].includes(status)) ||
          (state.filter === "warning" && (status === "warning" || order.warnings?.length)) ||
          (state.filter === "error" && (status === "error" || status === "failed" || order.errors?.length));
        const text = [order.order_id, order.id, order.customer_id, ...(order.files || []).map((file) => file.filename)].join(" ").toLowerCase();
        return filterOk && (!query || text.includes(query));
      });
    },
    selectedOrders: (state) => state.orders.filter((order) => state.selected.includes(String(order.order_id ?? order.id))),
    canPrint() { return this.selectedOrders.length > 0 && this.selectedOrders.every((order) => ["passed", "warning", "completed"].includes(order.status)); },
  },
  actions: {
    setFilter(value) { this.filter = value; localStorage.setItem("im-filter", value); },
    async initialize() {
      this.loading = true; this.error = "";
      try {
        const [config, runs] = await Promise.all([api.config(), api.runs()]);
        this.config = config;
        this.runs = list(runs);
        // A refresh starts a fresh operator session.  Restore only work that
        // is genuinely still in progress; completed runs must not repopulate
        // counters or the active queue until the operator starts a new check.
        const candidate = this.runs.find((item) => running(item.status));
        if (candidate) await this.selectRun(candidate.id);
        else {
          this.activeRun = null;
          this.orders = [];
          this.selected = [];
        }
      } catch (error) { this.error = error.message; }
      finally { this.loading = false; }
    },
    async selectRun(id) {
      this.stopEvents?.(); this.stopEvents = null;
      const [run, orders] = await Promise.all([api.run(id), api.orders(id).catch(() => ({ items: [] }))]);
      this.activeRun = run;
      this.orders = decorateOrders(orders, id);
      this.selected = [];
      if (running(run.status)) this.listen(id);
    },
    listen(id) {
      this.stopEvents = connectRunEvents(id, {
        onState: async (state) => {
          this.connection = state;
          if (state === "reconnecting") await this.resync(id).catch(() => {});
        },
        onEvent: (event) => this.applyEvent(event),
      });
    },
    async resync(id = this.activeRun?.id) {
      if (!id) return;
      const [run, orders] = await Promise.all([api.run(id), api.orders(id)]);
      this.activeRun = run; this.orders = decorateOrders(orders, id);
    },
    async refreshOrders(id = this.activeRun?.id) {
      if (!id) return;
      this.orders = decorateOrders(await api.orders(id), id);
    },
    applyEvent(event) {
      this.events.unshift(event);
      this.events = this.events.slice(0, 100);
      if (event.run_id && this.activeRun && String(event.run_id) !== String(this.activeRun.id)) return;
      if (event.processed != null && event.total) this.activeRun.progress = Math.round(event.processed / event.total * 100);
      Object.assign(this.activeRun || {}, event.run || {});
      const orderData = event.order ? decorateOrder(event.order, this.activeRun?.id) : null;
      if (orderData) {
        const id = String(orderData.order_id ?? orderData.id);
        const index = this.orders.findIndex((item) => String(item.order_id ?? item.id) === id);
        if (index < 0) this.orders.unshift(orderData);
        else this.orders.splice(index, 1, { ...this.orders[index], ...orderData });
      } else if (event.order_id) {
        const id = String(event.order_id);
        const index = this.orders.findIndex((item) => String(item.order_id ?? item.id) === id);
        if (index >= 0 && event.status) {
          this.orders.splice(index, 1, { ...this.orders[index], status: event.status });
        }
      }
      if (["run.completed", "run.failed", "run.cancelled"].includes(event.type)) {
        this.stopEvents?.();
        this.stopEvents = null;
        this.connection = "closed";
        this.resync().catch(() => {});
      }
    },
    async start(options) {
      this.loading = true;
      try {
        const run = await api.start(options);
        this.drawerOpen = false;
        await this.selectRun(run.id);
        await this.refreshRuns();
      } finally {
        this.loading = false;
      }
    },
    async refreshRuns() { this.runs = list(await api.runs()); },
    async cancel() { if (this.activeRun) { await api.cancel(this.activeRun.id); await this.resync(); } },
    toggle(order) {
      const id = String(order.order_id ?? order.id);
      this.selected = this.selected.includes(id) ? this.selected.filter((item) => item !== id) : [...this.selected, id];
    },
    toggleAllFiltered() {
      const ids = this.filteredOrders.map((order) => String(order.order_id ?? order.id));
      const allSelected = ids.length > 0 && ids.every((id) => this.selected.includes(id));
      this.selected = allSelected
        ? this.selected.filter((id) => !ids.includes(id))
        : [...new Set([...this.selected, ...ids])];
    },
    clearSelection() { this.selected = []; },
    setReturnComment(order, comment) {
      const id = String(order.order_id ?? order.id);
      if (comment) this.returnComments[id] = comment;
      else delete this.returnComments[id];
    },
    async decide(order, decision) {
      await api.correction(this.activeRun.id, order.order_id ?? order.id, { decision });
      await this.resync();
    },
    async act(action, comment) {
      const order_ids = [...this.selected];
      const run_id = this.activeRun?.id;
      const responses = action === "print"
        ? [await api.preparePrint({ order_ids, run_id })]
        : await Promise.all(order_ids.map((orderId) => api.prepareReject({
          order_ids: [orderId],
          run_id,
          comment: [this.returnComments[orderId], comment].filter(Boolean).join("\n"),
        })));
      const result = responses.flatMap((response) => list(response));
      for (const item of result) {
        this.actionResults[String(item.order_id)] = item;
        const order = this.orders.find((value) => String(value.order_id ?? value.id) === String(item.order_id));
        if (order) order.action_result = item;
      }
      if (action === "reject") order_ids.forEach((id) => delete this.returnComments[id]);
      this.clearSelection();
      // Print/reject preparation changes order actions only. Keep the active
      // run object intact so its verification progress cannot jump or reset.
      await this.refreshOrders(run_id);
      return result;
    },
  },
});
