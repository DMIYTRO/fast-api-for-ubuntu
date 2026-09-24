import { defineStore } from "pinia";
import { api } from "../services/api.js";
import { connectRunEvents } from "../services/events.js";
import { applyRunFileProgressEvent, createRunFileProgress } from "./runFileProgress.js";
import { orderIdentity, sameOrderIdentity } from "./orderIdentity.js";

const list = (data) => Array.isArray(data) ? data : (data?.items || data?.orders || []);
const running = (status) => ["queued", "running", "waiting_confirmation", "cancelling"].includes(status);
const terminalStatuses = ["accepted_for_print", "returned_for_rework"];
const printableStatuses = ["passed", "warning", "completed"];
const pitstopPendingStatuses = ["queued", "pending", "running", "checking", "processing"];
let searchDebounceTimer;
let ordersRequestSequence = 0;
let orderEventRevision = 0;
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
  const fileOrderId = String(order.aggregate_id ?? (order.customer_id != null ? `${order.customer_id}:${orderId}` : orderId));
  const previews = order.preview_paths || [];
  const files = (order.files || order.file_results || []).map((file, index) => {
    const parsed = file.parsed || {};
    const side = file.side || parsed.side;
    const id = String(file.id || `${runId}:${fileOrderId}:${index}`);
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
    runs: [], activeRun: null, fileProgress: createRunFileProgress(), orders: [], config: null, loading: false, error: "",
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
    selectedOrders: (state) => state.selected.map((id) => state.selectedOrderSnapshots[id] || state.orders.find((order) => orderIdentity(order) === String(id) || String(order.order_id ?? order.id) === String(id))).filter(Boolean),
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
      const eventRevision = orderEventRevision;
      const result = await api.orders(runId, {
        page, page_size: this.pageSize, status: this.filter,
        search: this.search.trim(), active_only: true,
      });
      if (requestSequence !== ordersRequestSequence || eventRevision !== orderEventRevision || String(this.activeRun?.id) !== String(runId)) return;
      const totalPages = result.total_pages || 1;
      if (page > totalPages) return this.loadOrders(totalPages);
      this.page = result.page || page;
      this.orders = decorateOrders(result, runId);
      this.pageInfo = { total: result.total || 0, total_pages: result.total_pages || 1, counts: result.counts || {} };
      for (const order of this.orders) {
        const id = orderIdentity(order);
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
      this.fileProgress = createRunFileProgress(run.id);
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
      applyRunFileProgressEvent(this.fileProgress, event);
      if (event.processed != null && event.total) this.activeRun.progress = Math.round(event.processed / event.total * 100);
      Object.assign(this.activeRun || {}, event.run || {});
      const orderData = event.order ? decorateOrder(event.order, this.activeRun?.id) : null;
      if (orderData || event.order_id != null) orderEventRevision += 1;
      if (orderData) {
        const id = orderIdentity(orderData);
        let index = this.orders.findIndex((item) => sameOrderIdentity(item, orderData));
        if (index < 0 && !orderData.aggregate_id && orderData.customer_id == null) {
          const matches = this.orders.map((item, itemIndex) => ({ item, itemIndex }))
            .filter(({ item }) => String(item.order_id ?? item.id) === String(orderData.order_id ?? orderData.id));
          if (matches.length === 1) index = matches[0].itemIndex;
        }
        const resolvedId = index >= 0 ? orderIdentity(this.orders[index]) : id;
        if (index < 0) {
          const selectedKey = Object.keys(this.selectedOrderSnapshots).find((key) => sameOrderIdentity(this.selectedOrderSnapshots[key], orderData));
          const selectedSnapshot = this.selectedOrderSnapshots[selectedKey || id];
          if (selectedSnapshot) {
            const updatedSelection = { ...selectedSnapshot, ...orderData };
            const snapshotId = selectedKey || id;
            this.selectedOrderSnapshots[snapshotId] = updatedSelection;
            this._dropSelectionWhenPrintBecomesBlocked(snapshotId, selectedSnapshot, updatedSelection);
          }
          const status = orderData.status || "detected";
          const terminal = terminalStatuses.includes(status);
          const query = this.search.trim().toLowerCase();
          const text = [orderData.order_id, orderData.id, orderData.customer_id, ...(orderData.files || []).map((file) => file.filename)].join(" ").toLowerCase();
          const matchesPage = !terminal && matchesStatusFilter(orderData, this.filter) && (!query || text.includes(query));
          if (matchesPage && this.page === 1) {
            // New SSE orders append in discovery order; unshift would replace
            // the first visible card on every completed order. The next API
            // refresh supplies the canonical page in the same stable order.
            if (this.orders.length < this.pageSize) this.orders.push(orderData);
          }
        } else {
          const previous = this.orders[index];
          const updated = { ...previous, ...orderData };
          this.orders.splice(index, 1, updated);
          if (this.selected.includes(resolvedId)) this.selectedOrderSnapshots[resolvedId] = updated;
          this._dropSelectionWhenPrintBecomesBlocked(resolvedId, previous, updated);
        }
      } else if (event.order_id != null) {
        const eventOrder = { aggregate_id: event.aggregate_id, customer_id: event.customer_id, order_id: event.order_id, id: event.id };
        const id = orderIdentity(eventOrder);
        let index = this.orders.findIndex((item) => sameOrderIdentity(item, eventOrder));
        if (index < 0 && event.aggregate_id == null && event.customer_id == null) {
          const matches = this.orders.map((item, itemIndex) => ({ item, itemIndex }))
            .filter(({ item }) => String(item.order_id ?? item.id) === String(event.order_id));
          if (matches.length === 1) index = matches[0].itemIndex;
        }
        if (event.status) {
          const selectedKey = Object.keys(this.selectedOrderSnapshots).find((key) => sameOrderIdentity(this.selectedOrderSnapshots[key], eventOrder));
          const snapshotId = selectedKey || id;
          const previous = (index >= 0 ? this.orders[index] : null) || this.selectedOrderSnapshots[snapshotId];
          if (previous) {
            const updated = { ...previous, status: event.status };
            if (index >= 0) this.orders.splice(index, 1, updated);
            const resolvedId = index >= 0 ? orderIdentity(this.orders[index]) : snapshotId;
            if (this.selected.includes(resolvedId)) this.selectedOrderSnapshots[resolvedId] = updated;
            this._dropSelectionWhenPrintBecomesBlocked(resolvedId, previous, updated);
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
      const id = orderIdentity(order);
      if (this.selected.includes(id)) {
        this.selected = this.selected.filter((item) => item !== id);
        delete this.selectedOrderSnapshots[id];
      } else {
        this.selected = [...this.selected, id];
        this.selectedOrderSnapshots[id] = order;
      }
    },
    toggleAllFiltered() {
      const ids = this.filteredOrders.map((order) => orderIdentity(order));
      const allSelected = ids.length > 0 && ids.every((id) => this.selected.includes(id));
      if (allSelected) {
        this.selected = this.selected.filter((id) => !ids.includes(id));
        ids.forEach((id) => delete this.selectedOrderSnapshots[id]);
      } else {
        this.selected = [...new Set([...this.selected, ...ids])];
        this.filteredOrders.forEach((order) => {
          const id = orderIdentity(order);
          this.selectedOrderSnapshots[id] = order;
        });
      }
    },
    clearSelection() { this.selected = []; this.selectedOrderSnapshots = {}; },
    returnDesignEnabled(order) {
      const id = orderIdentity(order);
      return this.returnDesign[id]?.design !== false;
    },
    returnDesignCost(order) {
      const id = orderIdentity(order);
      return this.returnDesign[id]?.design_cost ?? "0";
    },
    setReturnDesign(order, design) {
      const id = orderIdentity(order);
      const designCost = this.returnDesign[id]?.design_cost ?? "0";
      this.returnDesign[id] = { design, design_cost: designCost };
    },
    setReturnDesignCost(order, designCost) {
      const id = orderIdentity(order);
      this.returnDesign[id] = {
        design: this.returnDesign[id]?.design !== false,
        design_cost: designCost || "0",
      };
    },
    setReturnComment(order, comment) {
      const id = orderIdentity(order);
      if (comment) this.returnComments[id] = comment;
      else delete this.returnComments[id];
    },
    async decide(order, decision) {
      await api.correction(this.activeRun.id, order.aggregate_id ?? order.order_id ?? order.id, { decision });
      await this.resync();
    },
    async act(action, comment, conflictStrategy = "fail") {
      const selectedOrders = this.selectedOrders;
      const order_ids = selectedOrders.map((order) => order.aggregate_id ?? order.order_id ?? order.id);
      const run_id = this.activeRun?.id;
      const isPrint = ["print", "force-print"].includes(action);
      const responses = isPrint
        ? [await api.preparePrint({
          order_ids,
          run_id,
          conflict_strategy: conflictStrategy,
          confirm_failed_processing: action === "force-print",
        })]
        : await Promise.all(selectedOrders.map((order) => {
          const identity = orderIdentity(order);
          const orderId = order.aggregate_id ?? order.order_id ?? order.id;
          return api.prepareReject({
            order_ids: [orderId],
            run_id,
            comment: [this.returnComments[identity] ?? this.returnComments[String(order.order_id ?? order.id)], comment].filter(Boolean).join("\n"),
            ...(this.returnDesign[identity] || this.returnDesign[String(order.order_id ?? order.id)] || { design: true, design_cost: "0" }),
            conflict_strategy: conflictStrategy,
          });
        }));
      const result = responses.flatMap((response) => list(response));
      const resultIdentity = (item) => {
        const direct = orderIdentity(item);
        if (direct && !direct.startsWith("legacy:")) return direct;
        const matches = selectedOrders.filter((order) =>
          String(order.order_id ?? order.id) === String(item.order_id ?? item.id)
          || String(order.aggregate_id ?? "") === String(item.order_id ?? item.id)
        );
        return matches.length === 1 ? orderIdentity(matches[0]) : direct || String(item.order_id);
      };
      for (const item of result) {
        const identity = resultIdentity(item);
        this.actionResults[identity] = item;
        const order = this.orders.find((value) => orderIdentity(value) === identity);
        if (order) order.action_result = item;
      }
      const conflict = result.find((item) => item.status === "conflict");
      if (conflict) {
        this.conflictPrompt = {
          action,
          comment,
          orderId: resultIdentity(conflict),
          conflict: conflict.conflict,
        };
        return result;
      }
      this.conflictPrompt = null;
      const completedIds = new Set(result
        .filter((item) => item.status === "prepared")
        .map(resultIdentity));
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
          (order) => orderIdentity(order) === id
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
