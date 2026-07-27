import { createRouter, createWebHistory } from "vue-router";
import LoginView from "../views/LoginView.vue";
import DashboardView from "../views/DashboardView.vue";
import HistoryView from "../views/HistoryView.vue";
import { useAuthStore } from "../stores/auth.js";

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/login", name: "login", component: LoginView, meta: { guest: true } },
    { path: "/", name: "dashboard", component: DashboardView },
    { path: "/history", name: "history", component: HistoryView },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

router.beforeEach(async (to) => {
  const auth = useAuthStore();
  if (!auth.checked) await auth.restore();
  if (!to.meta.guest && !auth.authenticated) return { name: "login", query: { next: to.fullPath } };
  if (to.meta.guest && auth.authenticated) return { name: "dashboard" };
});

export default router;
