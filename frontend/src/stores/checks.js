import { defineStore } from "pinia";
import { api } from "../services/api.js";
import { connectRunEvents } from "../services/events.js";
import { previewQueue } from "../services/previewQueue.js";

const list = (data) => Array.isArray(data) ? data : (data?.items || data?.orders || []);
const running = (status) => ["queued", "running", "waiting_confirmation", "cancelling"].includes(status);
const terminalStatuses = ["accepted_for_print", "returned_for_rework"];
const printableStatuses = ["passed", "warning", "completed"];
const pitstopPendingStatuses = ["queued", "pending", "running", "checking", "processing"];

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
    connection: "closed", selected: [], filter: localStorage.getItem("im-filter") || "passed",
    search: "", page: 1, pageSize: [10, 50, 100].includes(Number(localStorage.getItem("im-page-size"))) ? Number(localStorage.getItem("im-page-size")) : 10,
    total: 0, totalPages: 1, counts: {}, paginationLoaded: false, pageLoading: false, requestSequence: 0,
    previewSweepKey: "", previewSweepSequence: 0, previewSweepCursor: 0, previewSweepPages: 0, previewSweepRunning: false,
    events: [], drawerOpen: false, stopEvents: null, actionResults: {}, returnComments: {}, returnDesign: {}, conflictPrompt: null,
  }),
  getters: {
    filteredOrders(state) {
      if (state.paginationLoaded) return state.orders.filter((order) => !terminalStatuses.includes(order.status));
      const query = state.search.trim().toLowerCase();
      return state.orders.filter((order) => {
        const status = order.status || "detected";
        // This dashboard is an active work queue.  Orders already handed to
        // print or returned for rework belong to their file folders/history,
        // not to any of the active tabs (including "Все").
        if (terminalStatuses.includes(status)) return false;
        const filterOk = matchesStatusFilter(order, state.filter);
        const text = [order.order_id, order.id, order.customer_id, ...(order.files || []).map((file) => file.filename)].join(" ").toLowerCase();
        return filterOk && (!query || text.includes(query));
      });
    },
    selectedOrders: (state) => state.orders.filter((order) => state.selected.includes(String(order.order_id ?? order.id))),
    canPrint() { return this.selectedOrders.length > 0 && this.selectedOrders.every(isOrderPrintable); },
    canForcePrint() {
      return this.selectedOrders.some(isOrderForcePrintable)
        && this.selectedOrders.every((order) => isOrderPrintable(order) || isOrderForcePrintable(order));
    },
  },
  actions: {
    setFilter(value) {
      this.filter = value;
      localStorage.setItem("im-filter", value);
      if (this.activeRun) { this.page = 1; this.selected = []; this.refreshOrders(); }
    },
    setSearch(value) {
      this.search = value;
      if (!this.activeRun) return;
      this.page = 1;
      this.selected = [];
      clearTimeout(this._searchTimer);
      this._searchTimer = setTimeout(() => this.refreshOrders(), 300);
    },
    setPage(page) {
      if (page < 1 || page > this.totalPages || page === this.page) return;
      this.page = page;
      this.selected = [];
      this.refreshOrders();
    },
    setPageSize(size) {
      const value = Number(size);
      if (![10, 50, 100].includes(value)) return;
      this.pageSize = value;
      localStorage.setItem("im-page-size", String(value));
      this.page = 1;
      this.selected = [];
      this.resetPreviewSweep();
      if (this.activeRun) this.refreshOrders();
    },
    resetPreviewSweep() {
      this.previewSweepKey = "";
      this.previewSweepSequence++;
      this.previewSweepCursor = 0;
      this.previewSweepPages = 0;
      this.previewSweepRunning = false;
      previewQueue.clear();
    },
    async warmPreviewPages(id, key, sequence) {
      if (this.previewSweepRunning) return;
      this.previewSweepRunning = true;
      try {
        while (this.previewSweepCursor < this.previewSweepPages) {
          if (this.previewSweepSequence !== sequence || this.previewSweepKey !== key) return;
          const page = this.previewSweepCursor + 1;
          await previewQueue.whenIdle();
          if (this.previewSweepSequence !== sequence || this.previewSweepKey !== key) return;
          const result = await api.orders(id, {
            page, page_size: this.pageSize, status: "all", search: "", active_only: true,
          });
          if (this.previewSweepSequence !== sequence || this.previewSweepKey !== key) return;
          previewQueue.enqueue(decorateOrders(result, id));
          await previewQueue.whenIdle();
          this.previewSweepCursor = page;
        }
      } catch {
        return;
      } finally {
        if (this.previewSweepSequence === sequence && this.previewSweepKey === key) {
          this.previewSweepRunning = false;
          if (this.previewSweepCursor < this.previewSweepPages) {
            queueMicrotask(() => this.warmPreviewPages(id, key, sequence));
          }
        }
      }
    },
    async loadOrders(id = this.activeRun?.id) {
      if (!id) return;
      const sequence = ++this.requestSequence;
      this.pageLoading = true;
      try {
        const result = await api.orders(id, {
          page: this.page, page_size: this.pageSize, status: this.filter,
          search: this.search.trim(), active_only: true,
        });
        if (sequence !== this.requestSequence || String(this.activeRun?.id) !== String(id)) return;
        this.orders = decorateOrders(result, id);
        this.total = result.total ?? this.orders.length;
        this.totalPages = result.total_pages ?? 1;
        this.counts = result.counts || {};
        this.paginationLoaded = true;
        previewQueue.prioritize(this.orders);
        const sweepKey = [id, this.pageSize].join("|");
        if (this.previewSweepKey !== sweepKey) {
          this.previewSweepKey = sweepKey;
          this.previewSweepSequence++;
          this.previewSweepCursor = 0;
          this.previewSweepPages = 0;
          this.previewSweepRunning = false;
        }
        const allOrders = Number(this.counts.all ?? this.total);
        this.previewSweepPages = Math.max(
          this.previewSweepPages, Math.ceil(allOrders / this.pageSize),
        );
        if (this.previewSweepCursor < this.previewSweepPages && !this.previewSweepRunning) {
          this.warmPreviewPages(id, sweepKey, this.previewSweepSequence);
        }
        this.selected = this.selected.filter((selectedId) => this.orders.some(
          (order) => String(order.order_id ?? order.id) === selectedId
        ));
        if (this.page > this.totalPages) {
          this.page = this.totalPages;
          await this.loadOrders(id);
        }
      } finally {
        if (sequence === this.requestSequence) this.pageLoading = false;
      }
    },
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
          this.paginationLoaded = false;
          this.resetPreviewSweep();
        }
      } catch (error) { this.error = error.message; }
      finally { this.loading = false; }
    },
    async selectRun(id) {
      this.stopEvents?.(); this.stopEvents = null;
      this.requestSequence++;
      this.resetPreviewSweep();
      this.paginationLoaded = false;
      this.page = 1;
      this.selected = [];
      const run = await api.run(id);
      this.activeRun = run;
      await this.loadOrders(id);
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
      const run = await api.run(id);
      if (String(this.activeRun?.id) !== String(id)) return;
      this.activeRun = run;
      await this.loadOrders(id);
    },
    async refreshOrders(id = this.activeRun?.id) {
      await this.loadOrders(id);
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
          if (this.paginationLoaded) this.queuePageRefresh();
          else this.orders.unshift(orderData);
        }
        else {
          const previous = this.orders[index];
          const updated = { ...previous, ...orderData };
          this.orders.splice(index, 1, updated);
          this._dropSelectionWhenPrintBecomesBlocked(id, previous, updated);
          if (this.paginationLoaded && previous.status !== updated.status) this.queuePageRefresh();
        }
      } else if (event.order_id) {
        const id = String(event.order_id);
        const index = this.orders.findIndex((item) => String(item.order_id ?? item.id) === id);
        if (index >= 0 && event.status) {
          const previous = this.orders[index];
          const updated = { ...previous, status: event.status };
          this.orders.splice(index, 1, updated);
          this._dropSelectionWhenPrintBecomesBlocked(id, previous, updated);
          if (this.paginationLoaded && previous.status !== updated.status) this.queuePageRefresh();
        }
      }
      if (["run.completed", "run.failed", "run.cancelled"].includes(event.type)) {
        this.stopEvents?.();
        this.stopEvents = null;
        this.connection = "closed";
        this.resync().catch(() => {});
      }
    },
    queuePageRefresh() {
      if (!this.activeRun) return;
      this.previewSweepKey = "";
      clearTimeout(this._pageRefreshTimer);
      this._pageRefreshTimer = setTimeout(() => this.refreshOrders().catch(() => {}), 400);
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
      }
    },
  },
});
