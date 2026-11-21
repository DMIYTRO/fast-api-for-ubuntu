import { afterEach, expect, it, vi } from "vitest";
import { createApp } from "vue";
import { createPinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import HistoryView from "./HistoryView.vue";
import { api } from "../services/api.js";

const flushPromises = () => new Promise((resolve) => setTimeout(resolve, 0));
let app; let host;
afterEach(() => { app?.unmount(); host?.remove(); vi.restoreAllMocks(); });

it("loads the order journal lazily on first open and reuses it", async () => {
  vi.spyOn(api, "history").mockResolvedValue({
    items: [{ id: 7, action: "print", status: "prepared", created_at: "2026-11-21T10:05:00+00:00", order_id: "1001", customer_id: "42", comment: null, previews: [] }],
    page: 1, total_pages: 1, total: 1,
  });
  const journal = vi.spyOn(api, "orderJournal").mockResolvedValue({
    entries: [{ kind: "auto_change", code: "auto:icc_removed", source: "callas", severity: null, title: "Снят ICC-профиль", at: null, outcome: "converted", data: {} }],
  });
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: { template: "<div />" } }, { path: "/history", component: HistoryView }] });
  host = document.createElement("div"); document.body.append(host);
  app = createApp(HistoryView); app.use(createPinia()); app.use(router); app.mount(host);
  await flushPromises();
  expect(journal).not.toHaveBeenCalled();
  const toggle = () => host.querySelector(".history-journal-toggle");
  toggle().click();
  await flushPromises();
  expect(journal).toHaveBeenCalledWith(7);
  expect(host.textContent).toContain("Исправлено автоматически");
  expect(host.textContent).toContain("Снят ICC-профиль");
  toggle().click();
  await flushPromises();
  expect(host.querySelector(".history-journal")).toBeNull();
  toggle().click();
  await flushPromises();
  expect(journal).toHaveBeenCalledTimes(1);
  expect(host.textContent).toContain("Снят ICC-профиль");
});

it("shows a load error and retries on the next open", async () => {
  vi.spyOn(api, "history").mockResolvedValue({ items: [{ id: 3, action: "reject", status: "prepared", created_at: "2026-11-21T10:05:00+00:00", order_id: "2", customer_id: "", comment: "", previews: [] }], page: 1, total_pages: 1, total: 1 });
  const journal = vi.spyOn(api, "orderJournal").mockRejectedValueOnce(new Error("Запись истории или её заказ не найдены.")).mockResolvedValue({ entries: [] });
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: "/", component: { template: "<div />" } }, { path: "/history", component: { template: "<div />" } }] });
  host = document.createElement("div"); document.body.append(host);
  app = createApp(HistoryView); app.use(createPinia()); app.use(router); app.mount(host);
  await flushPromises();
  host.querySelector(".history-journal-toggle").click();
  await flushPromises();
  expect(host.querySelector("[role=alert]").textContent).toContain("не найдены");
  host.querySelector(".history-journal-toggle").click();
  host.querySelector(".history-journal-toggle").click();
  await flushPromises();
  expect(journal).toHaveBeenCalledTimes(2);
  expect(host.textContent).toContain("Записей в журнале нет.");
});
