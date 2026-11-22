import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApp, nextTick } from "vue";
import { createPinia, setActivePinia } from "pinia";
import OrderFetch from "./OrderFetch.vue";
import AppHeader from "./AppHeader.vue";
import { ApiError } from "../services/api.js";
import { fetchProgressLabel, formatDuration, formatMb, orderFetchApi, summarizeFetch } from "../services/orderFetch.js";
import { ALREADY_RUNNING_MESSAGE, ORDER_FETCH_POLL_MS, useOrderFetchStore } from "../stores/orderFetch.js";

const MB = 1024 * 1024;
const copying = (over = {}) => ({
  job_id: "ab12", state: "copying", total_files: 5, done_files: 2,
  total_bytes: 50 * MB, done_bytes: 12.5 * MB, bytes_per_second: 10.3 * MB,
  current: ["a.pdf"], result: null, ...over,
});
const done = (result = {}) => ({
  job_id: "ab12", state: "done", total_files: 3, done_files: 3, total_bytes: 3 * MB, done_bytes: 3 * MB,
  result: { moved: [], copied: [], skipped: [], failed: [], source_errors: [], ...result },
});
const flush = async () => { for (let i = 0; i < 5; i += 1) await Promise.resolve(); await nextTick(); };

let pinia;
const mounted = [];
function mount(component, props = {}) {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(component, props);
  app.use(pinia);
  app.component("RouterLink", { render: () => null }); // AppHeader без роутера
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return host;
}

beforeEach(() => {
  pinia = createPinia();
  setActivePinia(pinia);
  vi.useFakeTimers();
  vi.restoreAllMocks();
});
afterEach(() => {
  useOrderFetchStore().stop();
  mounted.splice(0).forEach((unmount) => unmount());
  vi.useRealTimers();
});

describe("order fetch formatting", () => {
  it("formats copy progress in Russian with МБ and speed", () => {
    expect(fetchProgressLabel(copying())).toBe("Копирование: 2 из 5 файлов · 12,5 из 50,0 МБ · 10,3 МБ/с");
    expect(fetchProgressLabel({ state: "scanning" })).toBe("Поиск файлов в сетевых папках…");
    expect(formatMb(345.4 * MB)).toBe("345");
    expect(formatMb(undefined)).toBe("0,0");
  });

  it("summarizes moved, skipped by reason and failed files", () => {
    const summary = summarizeFetch(done({
      moved: [{ name: "a.pdf" }, { name: "b.pdf" }],
      skipped: [{ name: "c.pdf", code: "zero_size" }, { name: "d.pdf", code: "still_writing" }, { name: "e", code: "subfolder" }, { name: "f.pdf", code: "zero_size" }],
      failed: [{ name: "g.pdf", code: "name_conflict", reason: "в рабочей папке уже есть другой файл с таким именем" }],
    }));
    expect(summary.headline).toBe("Перенесено 2 · пропущено 4 (пустые 2, ещё записываются 1, подпапки 1) · ошибки 1");
    expect(summary.problems).toEqual(["g.pdf — в рабочей папке уже есть другой файл с таким именем"]);
    expect(summary.ok).toBe(false);
  });

  it("splits source errors by severity and says when there is nothing new", () => {
    const summary = summarizeFetch(done({
      source_errors: [
        { source: "/mnt/work20/WORK/offset/ORDER/CorelDRAW", code: "missing", severity: "warning", message: "Папка-источник не найдена: /mnt/work20/WORK/offset/ORDER/CorelDRAW" },
      ],
    }));
    expect(summary.headline).toBe("Новых файлов нет");
    expect(summary.ok).toBe(true);
    expect(summary.sourceWarnings.map((item) => item.text)).toEqual(["Папка-источник не найдена: /mnt/work20/WORK/offset/ORDER/CorelDRAW"]);
    expect(summary.sourceErrors).toEqual([]);

    // Четыре источника одной несмонтированной шары — одна строка.
    const unmountedError = (source) => ({ source, code: "unmounted", severity: "error", message: "Сетевая папка не смонтирована: /mnt/work20" });
    const unmounted = summarizeFetch(done({ source_errors: ["a", "b", "c", "d"].map((name) => unmountedError(`/mnt/work20/${name}`)) }));
    expect(unmounted.ok).toBe(false);
    expect(unmounted.sourceErrors.map((item) => item.text)).toEqual(["Сетевая папка не смонтирована: /mnt/work20"]);
  });

  it("reports a copy whose source could not be deleted", () => {
    const summary = summarizeFetch(done({
      moved: [{ name: "a.pdf" }],
      copied: [{ name: "b.pdf", code: "delete_failed", reason: "не удалось удалить исходник: нет доступа" }],
    }));
    expect(summary.headline).toContain("скопировано без удаления исходника 1");
    expect(summary.problems).toEqual(["b.pdf — не удалось удалить исходник: нет доступа"]);
    expect(summary.ok).toBe(false);
  });
  it("adds duration and average speed when files were transferred", () => {
    const summary = summarizeFetch(done({ moved: [{ name: "a.pdf" }], duration_seconds: 12.2, bytes_per_second: 54 * MB }));
    expect(summary.headline).toBe("Перенесено 1 · пропущено 0 · ошибки 0 · за 12 с · 54,0 МБ/с");
    expect(formatDuration(0.42)).toBe("0,4 с");
    expect(formatDuration(125)).toBe("2 мин 5 с");
    expect(summarizeFetch(done({ duration_seconds: 0.1, bytes_per_second: 0 })).headline).toBe("Новых файлов нет");
  });

  it("shows a cancelled fetch as interrupted with the same summary", () => {
    const summary = summarizeFetch({ ...done({ moved: [{ name: "a.pdf" }], duration_seconds: 3, bytes_per_second: 2 * MB }), state: "cancelled" });
    expect(summary.headline).toBe("Получение прервано · Перенесено 1 · пропущено 0 · ошибки 0 · за 3,0 с · 2,0 МБ/с");
    expect(summary.ok).toBe(false);
  });

  it("groups new skip codes, unknown ones by reason", () => {
    const summary = summarizeFetch(done({
      skipped: [
        { name: "a.pdf", code: "still_writing", reason: "Файл изменился во время переноса, повторите позже" },
        { name: "b.pdf", code: "still_writing", reason: "Файл ещё записывается" },
        { name: "c.pdf", code: "source_vanished", reason: "файл исчез (забрали другим способом)" },
        { name: "d.pdf", code: "brand_new", reason: "новая причина" },
        { name: "e.pdf", code: "brand_new", reason: "новая причина" },
      ],
      source_errors: [{ source: "/x", code: "vanished", severity: "warning", message: "Папка-источник пропала во время сканирования: /x" }],
    }));
    expect(summary.headline).toBe("Перенесено 0 · пропущено 5 (ещё записываются 2, исчезли из источника 1, новая причина 2) · ошибки 0");
    expect(summary.sourceWarnings.map((item) => item.text)).toEqual(["Папка-источник пропала во время сканирования: /x"]);
  });
});

describe("OrderFetch panel and header button", () => {
  it("starts on click, spins and disables the button while copying, then shows the summary", async () => {
    vi.spyOn(orderFetchApi, "start").mockResolvedValue({ job_id: "ab12", state: "scanning" });
    const status = vi.spyOn(orderFetchApi, "status")
      .mockResolvedValueOnce(copying())
      .mockResolvedValueOnce(done({
        moved: [{ name: "a.pdf" }],
        failed: [{ name: "b.pdf", reason: "нет места в рабочей папке" }],
        source_errors: [{ source: "/mnt/work20", severity: "error", message: "Сетевая папка не смонтирована: /mnt/work20" }],
      }));
    const store = useOrderFetchStore();
    const header = mount(AppHeader, { showFetch: true, onFetchOrders: () => store.start() });
    const panel = mount(OrderFetch);
    const button = [...header.querySelectorAll("button")].find((item) => item.textContent.includes("Получить заказ"));
    expect(button).toBeTruthy();

    button.click();
    await flush();
    expect(orderFetchApi.start).toHaveBeenCalledTimes(1);
    expect(panel.querySelector(".run-throbber")).not.toBeNull();
    expect(panel.textContent).toContain("Поиск файлов");

    await vi.advanceTimersByTimeAsync(ORDER_FETCH_POLL_MS);
    await flush();
    expect(panel.querySelector(".run-progress-label").textContent).toBe("Копирование: 2 из 5 файлов · 12,5 из 50,0 МБ · 10,3 МБ/с");
    expect(store.busy).toBe(true);

    await vi.advanceTimersByTimeAsync(ORDER_FETCH_POLL_MS);
    await flush();
    expect(panel.querySelector(".run-throbber")).toBeNull();
    expect(panel.querySelector(".run-progress-label").textContent).toBe("Перенесено 1 · пропущено 0 · ошибки 1");
    expect(panel.querySelector(".order-fetch-source.form-error").textContent).toBe("Сетевая папка не смонтирована: /mnt/work20");
    expect(panel.querySelector(".order-fetch-problems summary").textContent).toContain("(1)");
    expect(panel.querySelector(".order-fetch-problems li").textContent).toBe("b.pdf — нет места в рабочей папке");

    await vi.advanceTimersByTimeAsync(ORDER_FETCH_POLL_MS * 3);
    expect(status).toHaveBeenCalledTimes(2); // опрос прекращён после завершения
  });

  it("header button is disabled while a fetch runs and absent unless enabled", async () => {
    const busy = mount(AppHeader, { showFetch: true, fetchBusy: true });
    const button = [...busy.querySelectorAll("button")].find((item) => item.textContent.includes("Получить заказ"));
    expect(button.disabled).toBe(true);
    const plain = mount(AppHeader, {});
    expect(plain.textContent).not.toContain("Получить заказ");
  });

  it("shows a warning-level source error quietly, not as a failure", async () => {
    vi.spyOn(orderFetchApi, "start").mockResolvedValue(done({
      source_errors: [{ source: "/x", code: "missing", severity: "warning", message: "Папка-источник не найдена: /x" }],
    }));
    const panel = mount(OrderFetch);
    await useOrderFetchStore().start();
    await flush();
    expect(panel.querySelector(".run-progress-label").textContent).toBe("Новых файлов нет");
    expect(panel.querySelector(".run-progress-marker")).not.toBeNull();
    expect(panel.querySelector(".form-error")).toBeNull();
    expect(panel.querySelector(".order-fetch-warning").textContent).toBe("Папка-источник не найдена: /x");
  });

  it("picks up a fetch already running in another tab when the page opens", async () => {
    vi.spyOn(orderFetchApi, "status").mockResolvedValue(copying({ done_files: 4 }));
    const panel = mount(OrderFetch);
    await useOrderFetchStore().initialize();
    await flush();
    expect(panel.querySelector(".run-throbber")).not.toBeNull();
    expect(panel.textContent).toContain("Копирование: 4 из 5 файлов");
  });

  it("does not show an old finished fetch when the page opens", async () => {
    vi.spyOn(orderFetchApi, "status").mockResolvedValue(done({ moved: [{ name: "a.pdf" }] }));
    const panel = mount(OrderFetch);
    await useOrderFetchStore().initialize();
    await flush();
    expect(panel.querySelector(".order-fetch")).toBeNull();
  });

  it("on 409 says the fetch is already running and follows its progress", async () => {
    vi.spyOn(orderFetchApi, "start").mockRejectedValue(new ApiError("Получение заказов уже идёт", 409, {}));
    vi.spyOn(orderFetchApi, "status").mockResolvedValue(copying());
    const panel = mount(OrderFetch);
    await useOrderFetchStore().start();
    await flush();
    expect(panel.querySelector(".order-fetch-notice").textContent).toBe(ALREADY_RUNNING_MESSAGE);
    expect(panel.querySelector(".run-throbber")).not.toBeNull();
    expect(panel.textContent).toContain("Копирование: 2 из 5 файлов");
  });

  it("keeps polling through a network error and keeps the last progress", async () => {
    vi.spyOn(orderFetchApi, "start").mockResolvedValue(copying());
    const status = vi.spyOn(orderFetchApi, "status")
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(copying({ done_files: 3 }));
    const panel = mount(OrderFetch);
    await useOrderFetchStore().start();
    await vi.advanceTimersByTimeAsync(ORDER_FETCH_POLL_MS);
    await flush();
    expect(panel.textContent).toContain("Копирование: 2 из 5 файлов");
    await vi.advanceTimersByTimeAsync(ORDER_FETCH_POLL_MS);
    await flush();
    expect(status).toHaveBeenCalledTimes(2);
    expect(panel.textContent).toContain("Копирование: 3 из 5 файлов");
  });

  it("shows a start error without spinning", async () => {
    vi.spyOn(orderFetchApi, "start").mockRejectedValue(new ApiError("Не удалось запустить получение заказов: ошибка конфигурации", 500, {}));
    const panel = mount(OrderFetch);
    await useOrderFetchStore().start();
    await flush();
    expect(panel.querySelector(".run-throbber")).toBeNull();
    expect(panel.querySelector(".run-waiting-marker")).not.toBeNull();
    expect(panel.querySelector(".run-progress-label").textContent).toContain("ошибка конфигурации");
  });

  it("renders a cancelled fetch in the panel", async () => {
    vi.spyOn(orderFetchApi, "start").mockResolvedValue({ ...done({ moved: [{ name: "a.pdf" }], failed: [{ name: "b.pdf", reason: "отменено" }] }), state: "cancelled" });
    const panel = mount(OrderFetch);
    await useOrderFetchStore().start();
    await flush();
    expect(panel.querySelector(".run-throbber")).toBeNull();
    expect(panel.querySelector(".run-waiting-marker")).not.toBeNull();
    expect(panel.querySelector(".run-progress-label").textContent).toContain("Получение прервано");
    expect(panel.querySelector(".order-fetch-problems li").textContent).toBe("b.pdf — отменено");
  });
});
