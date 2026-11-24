import { afterEach, expect, it, vi } from "vitest";
import { createApp, nextTick } from "vue";
import { createPinia } from "pinia";
import { createMemoryHistory, createRouter } from "vue-router";
import DashboardView from "./DashboardView.vue";
import { api } from "../services/api.js";
import { orderFetchApi } from "../services/orderFetch.js";
import { ORDER_FETCH_POLL_MS } from "../stores/orderFetch.js";

const finished = {
  job_id: "fetch-1",
  state: "done",
  result: { moved: [{ name: "order.pdf" }] },
};
let app;
let host;

afterEach(() => {
  app?.unmount();
  host?.remove();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

async function mountDashboard() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/", component: DashboardView },
      { path: "/history", component: { template: "<div />" } },
    ],
  });
  await router.push("/");
  vi.useFakeTimers();
  vi.spyOn(api, "config").mockResolvedValue({ default_input_path: "/work/Input" });
  vi.spyOn(api, "runs").mockResolvedValue([]);
  vi.spyOn(api, "run").mockResolvedValue({ id: "check-1", status: "completed" });
  vi.spyOn(api, "orders").mockResolvedValue({ items: [] });
  vi.spyOn(api, "runKits").mockResolvedValue({ kits: {} });
  const startCheck = vi.spyOn(api, "start").mockResolvedValue({ id: "check-1" });

  host = document.createElement("div");
  document.body.append(host);
  app = createApp(DashboardView);
  app.use(createPinia()).use(router).mount(host);
  await vi.advanceTimersByTimeAsync(0);
  return startCheck;
}

it("starts one check of the working folder after the requested fetch finishes", async () => {
  vi.spyOn(orderFetchApi, "start").mockResolvedValue({ job_id: "fetch-1", state: "copying" });
  vi.spyOn(orderFetchApi, "status")
    .mockResolvedValueOnce({ state: "idle" })
    .mockResolvedValue(finished);
  const startCheck = await mountDashboard();

  host.querySelector(".header-actions .primary").click();
  await nextTick();
  const input = document.querySelector("#input-path");
  input.value = "/different/folder";
  input.dispatchEvent(new Event("input"));
  const profile = document.querySelector("#profile");
  profile.value = "offset";
  profile.dispatchEvent(new Event("change"));

  host.querySelector(".order-fetch-button").click();
  await vi.advanceTimersByTimeAsync(0);
  expect(startCheck).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(ORDER_FETCH_POLL_MS * 2);
  expect(startCheck).toHaveBeenCalledExactlyOnceWith({
    input_path: "/work/Input",
    direction: "offset",
    create_pdfs: true,
    generate_previews: true,
    copy_failures: true,
    correction_policy: "auto",
    approve_corrections: true,
    check_policy: "auto",
  });
});

it.each([
  ["an empty fetch", { ...finished, result: {} }],
  ["a partial failure", { ...finished, result: { ...finished.result, failed: [{ name: "other.pdf" }] } }],
  ["a cancelled fetch", { ...finished, state: "cancelled" }],
  ["a different job", { ...finished, job_id: "another-job" }],
])("does not start a check for %s", async (_name, snapshot) => {
  vi.spyOn(orderFetchApi, "start").mockResolvedValue({ job_id: "fetch-1", state: "scanning" });
  vi.spyOn(orderFetchApi, "status")
    .mockResolvedValueOnce({ state: "idle" })
    .mockResolvedValue(snapshot);
  const startCheck = await mountDashboard();

  host.querySelector(".order-fetch-button").click();
  await vi.advanceTimersByTimeAsync(0);
  await vi.advanceTimersByTimeAsync(ORDER_FETCH_POLL_MS * 2);
  expect(startCheck).not.toHaveBeenCalled();
});

it("reports an automatic check launch error after a successful immediate fetch", async () => {
  vi.spyOn(orderFetchApi, "start").mockResolvedValue(finished);
  vi.spyOn(orderFetchApi, "status").mockResolvedValue({ state: "idle" });
  const startCheck = await mountDashboard();
  startCheck.mockRejectedValueOnce(new Error("Папка недоступна"));

  host.querySelector(".order-fetch-button").click();
  await vi.advanceTimersByTimeAsync(0);
  expect(host.querySelector(".order-fetch-notice")?.textContent)
    .toContain("Не удалось автоматически запустить проверку: Папка недоступна");
});
