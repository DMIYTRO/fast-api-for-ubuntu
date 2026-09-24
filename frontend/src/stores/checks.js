import { defineStore } from "pinia";
import { api } from "../services/api.js";
import { connectRunEvents } from "../services/events.js";

const list = (data) => Array.isArray(data) ? data : (data?.items || data?.orders || []);
const running = (status) => ["queued", "running", "waiting_confirmation", "cancelling"].includes(status);
const terminalStatuses = ["accepted_for_print", "returned_for_rework"];
const printableStatuses = ["passed", "warning", "completed"];
const pitstopPendingStatuses = ["queued", "pending", "running", "checking", "processing"];
let searchDebounceTimer;
let ordersRequestSequence = 0;
let orderPageRefreshTimer;

export const matchesStatusFilter = (order, filter) => {
  const status = order?.status || "detected";
  if (filter === "all") return true;
  if (filter === "passed") return printableStatuses.includes(status);
  if (filter === "warning") return status === "warning";
  if (filter === "error") return ["error", "failed", "technical_error"].includes(status);
  return status === filter;
};

export const isOrderPrintable = (order) => {
  if (!printableStatuses.includes(order?.status)) return false;
  const pitstop = order?.pitstop;
  if (!pitstop) return true;
  const executionStatus = String(pitstop.execution_status || "").toLowerCase();
  const verdict = String(pitstop.verdict || "").toLowerCase();
  if (pitstopPendingStatuses.includes(executionStatus) || ["failed", "error", "technical_error"].includes(executionStatus)) return false;
  if (["error", "failed", "rejected", "technical_error"].includes(verdict)) return false;
  return executionStatus === "completed" && ["passed", "warning", "ok", "success"].includes(verdict);
};
export const isOrderForcePrintable = (order) => order?.status === "error";
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
    connection: "closed", selected: [], selectedOrderSnapshots: {}, filter: localStorage.getItem("im-filter") || "passed",
    search: "", page: 1, pageSize: 10, pageInfo: { total: 0, total_pages: 1, counts: {} },
    events: [], drawerOpen: false, stopEvents: null, actionResults: {}, returnComments: {}, returnDesign: {}, conflictPrompt: null,
  }),
  getters: {
    filteredOrders(state) {
      const query = state.search.trim().toLowerCase();
      return state.orders.filter((order) => {
        const status = order.status || "detected";
        if (terminalStatuses.includes(status)) return false;
        const filterOk = matchesStatusFilter(order, state.filter);
        const text = [order.order_id, order.id, order.customer_id, ...(order.files || []).map((file) => file.filename)].join(" ").toLowerCase();
        return filterOk && (!query || text.includes(query));
      });
    },
    selectedOrders: (state) => state.selected.map((id) => state.selectedOrderSnapshots[id] || state.orders.find((order) => String(order.order_id ?? order.id) === String(id))).filter(Boolean),
    canPrint() { return this.selectedOrders.length > 0 && this.selectedOrders.every(isOrderPrintable); },
    canForcePrint() {
      return this.selectedOrders.some(isOrderForcePrintable)
        && this.selectedOrders.every((order) => isOrderPrintable(order) || isOrderForcePrintable(order));
    },
  },
  actions: {
    setFilter(value) {
      this.filter = value; this.page = 1; localStorage.setItem("im-filter", value);
      return this.loadOrders(1);
    },
    setSearch(value) {
      this.search = value; this.page = 1;
      clearTimeout(searchDebounceTimer);
      searchDebounceTimer = setTimeout(() => this.loadOrders(1).catch(() => {}), 250);
    },
    async setPage(page) { return this.loadOrders(page); },
    async setPageSize(size) {
      if (![10, 50, 100].includes(Number(size))) return;
      this.pageSize = Number(size); this.page = 1;
      return this.loadOrders(1);
    },
    async loadOrders(page = this.page) {
      if (!this.activeRun?.id) return;
      const runId = this.activeRun.id;
      const requestSequence = ++ordersRequestSequence;
      const result = await api.orders(runId, {
        page, page_size: this.pageSize, status: this.filter,
        search: this.search.trim(), active_only: true,
      });
      if (requestSequence !== ordersRequestSequence || String(this.activeRun?.id) !== String(runId)) return;
      const totalPages = result.total_pages || 1;
      if (page > totalPages) return this.loadOrders(totalPages);
      this.page = result.page || page;
      this.orders = decorateOrders(result, runId);
      this.pageInfo = { total: result.total || 0, total_pages: result.total_pages || 1, counts: result.counts || {} };
      for (const order of this.orders) {
        const id = String(order.order_id ?? order.id);
        if (this.selected.includes(id)) this.selectedOrderSnapshots[id] = order;
      }
    },
    async initialize() {
      this.loading = true; this.error = "";
      try {
        const [config, runs] = await Promise.all([api.config(), api.runs()]);
        this.config = config;
        this.runs = list(runs);
        // Prefer work that is still running, otherwise restore the latest
        // saved run so its results and previews remain available after F5.
        const candidate = this.runs.find((item) => running(item.status)) || this.runs[0];
        if (candidate) await this.selectRun(candidate.id);
        else {
          this.activeRun = null;
          this.orders = [];
          this.selected = []; this.selectedOrderSnapshots = {};
          this.page = 1; this.pageInfo = { total: 0, total_pages: 1, counts: {} };
        }
      } catch (error) { this.error = error.message; }
      finally { this.loading = false; }
    },
    async selectRun(id) {
      this.stopEvents?.(); this.stopEvents = null;
      const [run] = await Promise.all([api.run(id, { include_orders: false })]);
      this.activeRun = run;
      this.page = 1; this.selected = []; this.selectedOrderSnapshots = {};
      await this.loadOrders(1);
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
      const [run] = await Promise.all([api.run(id, { include_orders: false })]);
      this.activeRun = run;
      await this.loadOrders(this.page);
    },
    async refreshOrders(id = this.activeRun?.id) {
      if (!id) return;
      await this.loadOrders(this.page);
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
        if (index < 0) {
          const selectedSnapshot = this.selectedOrderSnapshots[id];
          if (selectedSnapshot) {
            const updatedSelection = { ...selectedSnapshot, ...orderData };
            this.selectedOrderSnapshots[id] = updatedSelection;
            this._dropSelectionWhenPrintBecomesBlocked(id, selectedSnapshot, updatedSelection);
          }
          const status = orderData.status || "detected";
          const terminal = terminalStatuses.includes(status);
          const query = this.search.trim().toLowerCase();
          const text = [orderData.order_id, orderData.id, orderData.customer_id, ...(orderData.files || []).map((file) => file.filename)].join(" ").toLowerCase();
          const matchesPage = !terminal && matchesStatusFilter(orderData, this.filter) && (!query || text.includes(query));
          if (matchesPage && this.page === 1) {
            this.orders.unshift(orderData);
            if (this.orders.length > this.pageSize) this.orders.pop();
          }
        } else {
          const previous = this.orders[index];
          const updated = { ...previous, ...orderData };
          this.orders.splice(index, 1, updated);
          if (this.selected.includes(id)) this.selectedOrderSnapshots[id] = updated;
          this._dropSelectionWhenPrintBecomesBlocked(id, previous, updated);
        }
      } else if (event.order_id) {
        const id = String(event.order_id);
        const index = this.orders.findIndex((item) => String(item.order_id ?? item.id) === id);
        if (event.status) {
          const previous = this.orders[index] || this.selectedOrderSnapshots[id];
          if (previous) {
            const updated = { ...previous, status: event.status };
            if (index >= 0) this.orders.splice(index, 1, updated);
            if (this.selected.includes(id)) this.selectedOrderSnapshots[id] = updated;
            this._dropSelectionWhenPrintBecomesBlocked(id, previous, updated);
          }
        }
      }
      if (event.order || event.status) {
        clearTimeout(orderPageRefreshTimer);
        orderPageRefreshTimer = setTimeout(() => this.loadOrders(this.page).catch(() => {}), 600);
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
      if (this.selected.includes(id)) {
        this.selected = this.selected.filter((item) => item !== id);
        delete this.selectedOrderSnapshots[id];
      } else {
        this.selected = [...this.selected, id];
        this.selectedOrderSnapshots[id] = order;
      }
    },
    toggleAllFiltered() {
      const ids = this.filteredOrders.map((order) => String(order.order_id ?? order.id));
      const allSelected = ids.length > 0 && ids.every((id) => this.selected.includes(id));
      if (allSelected) {
        this.selected = this.selected.filter((id) => !ids.includes(id));
        ids.forEach((id) => delete this.selectedOrderSnapshots[id]);
      } else {
        this.selected = [...new Set([...this.selected, ...ids])];
        this.filteredOrders.forEach((order) => {
          const id = String(order.order_id ?? order.id);
          this.selectedOrderSnapshots[id] = order;
        });
      }
    },
    clearSelection() { this.selected = []; this.selectedOrderSnapshots = {}; },
    returnDesignEnabled(order) {
      const id = String(order.order_id ?? order.id);
      return this.returnDesign[id]?.design !== false;
    },
    returnDesignCost(order) {
      const id = String(order.order_id ?? order.id);
      return this.returnDesign[id]?.design_cost ?? "0";
    },
    setReturnDesign(order, design) {
      const id = String(order.order_id ?? order.id);
      const designCost = this.returnDesign[id]?.design_cost ?? "0";
      this.returnDesign[id] = { design, design_cost: designCost };
    },
    setReturnDesignCost(order, designCost) {
      const id = String(order.order_id ?? order.id);
      this.returnDesign[id] = {
        design: this.returnDesign[id]?.design !== false,
        design_cost: designCost || "0",
      };
    },
    setReturnComment(order, comment) {
      const id = String(order.order_id ?? order.id);
      if (comment) this.returnComments[id] = comment;
      else delete this.returnComments[id];
    },
    async decide(order, decision) {
      await api.correction(this.activeRun.id, order.order_id ?? order.id, { decision });
      await this.resync();
    },
    async act(action, comment, conflictStrategy = "fail") {
      const order_ids = [...this.selected];
      const run_id = this.activeRun?.id;
      const isPrint = ["print", "force-print"].includes(action);
      const responses = isPrint
        ? [await api.preparePrint({
          order_ids,
          run_id,
          conflict_strategy: conflictStrategy,
          confirm_failed_processing: action === "force-print",
        })]
        : await Promise.all(order_ids.map((orderId) => api.prepareReject({
          order_ids: [orderId],
          run_id,
          comment: [this.returnComments[orderId], comment].filter(Boolean).join("\n"),
          ...(this.returnDesign[orderId] || { design: true, design_cost: "0" }),
          conflict_strategy: conflictStrategy,
        })));
      const result = responses.flatMap((response) => list(response));
      for (const item of result) {
        this.actionResults[String(item.order_id)] = item;
        const order = this.orders.find((value) => String(value.order_id ?? value.id) === String(item.order_id));
        if (order) order.action_result = item;
      }
      const conflict = result.find((item) => item.status === "conflict");
      if (conflict) {
        this.conflictPrompt = {
          action,
          comment,
          orderId: String(conflict.order_id),
          conflict: conflict.conflict,
        };
        return result;
      }
      this.conflictPrompt = null;
      const completedIds = new Set(result
        .filter((item) => item.status === "prepared")
        .map((item) => String(item.order_id)));
      const terminalStatus = this._terminalStatusForAction(action);
      // The API response confirms that the files have already been moved.
      // Update the active queue immediately instead of waiting for a second
      // orders request, which can briefly return a stale run snapshot.
      this._markOrdersTerminal(completedIds, terminalStatus);
      if (action === "reject") {
        completedIds.forEach((id) => {
          delete this.returnComments[id];
          delete this.returnDesign[id];
        });
        this.selected = this.selected.filter((id) => !completedIds.has(String(id)));
        completedIds.forEach((id) => delete this.selectedOrderSnapshots[id]);
      } else {
        this.clearSelection();
      }
      // Print/reject preparation changes order actions only. Keep the active
      // run object intact so its verification progress cannot jump or reset.
      await this.refreshOrders(run_id);
      // Do not let a cached/stale orders response put a completed order back
      // into the active work queue.
      this._markOrdersTerminal(completedIds, terminalStatus);
      return result;
    },
    _markOrdersTerminal(orderIds, status) {
      orderIds.forEach((id) => {
        const index = this.orders.findIndex(
          (order) => String(order.order_id ?? order.id) === id
        );
        if (index >= 0) {
          this.orders.splice(index, 1, { ...this.orders[index], status });
        }
      });
    },
    _terminalStatusForAction(action) {
      return ["print", "force-print"].includes(action) ? "accepted_for_print" : "returned_for_rework";
    },
    _dropSelectionWhenPrintBecomesBlocked(id, previous, updated) {
      if (isOrderPrintable(previous) && !isOrderPrintable(updated)) {
        this.selected = this.selected.filter((selectedId) => String(selectedId) !== String(id));
        delete this.selectedOrderSnapshots[id];
      }
    },
  },
});
