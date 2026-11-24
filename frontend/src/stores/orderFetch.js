import { defineStore } from "pinia";
import { isFetchRunning, orderFetchApi } from "../services/orderFetch.js";

export const ORDER_FETCH_POLL_MS = 1000;
export const ALREADY_RUNNING_MESSAGE = "Получение заказов уже идёт";
let pollTimer = null;
let polling = false;

// «Получить заказ»: одна задача на сервер; прогресс опрашивается раз в секунду, пока она идёт.
// Задачу могли запустить в другой вкладке, поэтому initialize() подхватывает уже идущую.
export const useOrderFetchStore = defineStore("orderFetch", {
  state: () => ({
    snapshot: null,
    starting: false,
    visible: false,
    notice: "",
    error: "",
  }),
  getters: {
    running: (state) => isFetchRunning(state.snapshot),
    busy() { return this.starting || this.running; },
  },
  actions: {
    async initialize() {
      const previous = this.snapshot;
      try {
        const snapshot = await orderFetchApi.status();
        if (this.starting || this.snapshot !== previous) return;
        this.snapshot = snapshot;
        // Итог давней задачи при открытии страницы не показываем — только идущую.
        if (isFetchRunning(snapshot)) {
          this.visible = true;
          this.schedule();
        }
      } catch {
        // Нет связи или ручка недоступна — кнопка остаётся рабочей, ошибку покажет start().
      }
    },
    async start() {
      if (this.busy) return;
      this.starting = true;
      this.error = "";
      this.notice = "";
      try {
        const snapshot = await orderFetchApi.start();
        this.snapshot = snapshot;
        this.visible = true;
        if (isFetchRunning(snapshot)) this.schedule();
        return snapshot;
      } catch (error) {
        this.visible = true;
        if (error?.status === 409) {
          this.notice = ALREADY_RUNNING_MESSAGE;
          await this.refresh();
          this.schedule();
        } else {
          this.error = error?.message || "Не удалось запустить получение заказов";
        }
      } finally {
        this.starting = false;
      }
    },
    // Один опрос. Сетевая ошибка не меняет снимок: следующий тик повторит запрос.
    async refresh() {
      try {
        const snapshot = await orderFetchApi.status();
        const wasRunning = this.running;
        this.snapshot = snapshot;
        if (wasRunning && !this.running) this.notice = "";
      } catch (error) {
        if (error?.status === 401) this.stop();
      }
    },
    // Следующий опрос ставится после ответа на предыдущий, чтобы медленные ответы не копились.
    // После сетевой ошибки снимок прежний (идущий), и опрос продолжается.
    schedule() {
      if (pollTimer) return;
      polling = true;
      const timer = setTimeout(async () => {
        await this.refresh();
        if (pollTimer !== timer) return; // stop() или новый опрос уже сменили таймер
        pollTimer = null;
        if (polling && this.running) this.schedule();
        else polling = false;
      }, ORDER_FETCH_POLL_MS);
      pollTimer = timer;
    },
    stop() {
      polling = false;
      if (pollTimer) clearTimeout(pollTimer);
      pollTimer = null;
    },
    dismiss() {
      this.visible = false;
      this.notice = "";
      this.error = "";
    },
  },
});
