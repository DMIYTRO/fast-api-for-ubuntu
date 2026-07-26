import { defineStore } from "pinia";
import { api } from "../services/api.js";

export const useAuthStore = defineStore("auth", {
  state: () => ({ authenticated: false, checked: false, loading: false, error: "" }),
  actions: {
    async restore() {
      try {
        const data = await api.session();
        this.authenticated = data.authenticated !== false;
      } catch {
        this.authenticated = false;
      } finally {
        this.checked = true;
      }
    },
    async login(password) {
      this.loading = true; this.error = "";
      try {
        await api.login(password);
        this.authenticated = true; this.checked = true;
      } catch (error) {
        this.error = error.status === 429 ? "Слишком много попыток. Подождите и попробуйте снова." : "Неверный пароль";
        throw error;
      } finally { this.loading = false; }
    },
    async logout() {
      try { await api.logout(); } finally { this.authenticated = false; this.checked = true; }
    },
  },
});
