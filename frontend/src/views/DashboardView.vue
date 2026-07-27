<script setup>
import { onBeforeUnmount, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { useAuthStore } from "../stores/auth.js";
import { useChecksStore } from "../stores/checks.js";
import AppHeader from "../components/AppHeader.vue";
import StartCheckPanel from "../components/StartCheckPanel.vue";
import ActiveRun from "../components/ActiveRun.vue";
import RunHistory from "../components/RunHistory.vue";
import OrderFilters from "../components/OrderFilters.vue";
import OrderCard from "../components/OrderCard.vue";
import ActionBar from "../components/ActionBar.vue";

const auth = useAuthStore(); const checks = useChecksStore(); const router = useRouter();
const actionBusy = ref(false); const toast = ref("");
const showBackToTop = ref(false);
async function logout() { await auth.logout(); router.replace("/login"); }
async function act(action, comment) {
  actionBusy.value = true;
  try { await checks.act(action, comment); toast.value = action === "print" ? "Задание на печать подготовлено" : "Возврат подготовлен"; }
  catch (error) { toast.value = error.message; }
  finally { actionBusy.value = false; window.setTimeout(() => toast.value = "", 5000); }
}
async function resolveConflict(strategy) {
  const prompt = checks.conflictPrompt;
  if (!prompt) return;
  actionBusy.value = true;
  try {
    await checks.act(prompt.action, prompt.comment, strategy);
    checks.conflictPrompt = null;
    toast.value = strategy === "replace" ? "Существующий файл заменён" : "Файл сохранён под новым именем";
  } catch (error) {
    toast.value = error.message;
  } finally {
    actionBusy.value = false;
    window.setTimeout(() => toast.value = "", 5000);
  }
}
function expired() { auth.authenticated = false; router.replace("/login"); }
function updateBackToTop() { showBackToTop.value = window.scrollY > 600; }
function backToTop() { window.scrollTo({ top: 0, behavior: "smooth" }); }
onMounted(() => {
  window.addEventListener("auth:expired", expired);
  window.addEventListener("scroll", updateBackToTop, { passive: true });
  updateBackToTop();
  checks.initialize();
});
onBeforeUnmount(() => {
  window.removeEventListener("auth:expired", expired);
  window.removeEventListener("scroll", updateBackToTop);
  checks.stopEvents?.();
});
</script>

<template>
  <AppHeader :path="checks.activeRun?.input_path || checks.config?.default_input_path" :status="checks.activeRun?.status" :connection="checks.connection" @new-check="checks.drawerOpen = true" @logout="logout" />
  <main class="dashboard">
    <div v-if="checks.error" class="page-error" role="alert">{{ checks.error }} <button @click="checks.initialize()">Повторить</button></div>
    <template v-if="checks.activeRun">
      <ActiveRun :run="checks.activeRun" :events="checks.events" :connection="checks.connection" @cancel="checks.cancel" />
      <RunHistory :runs="checks.runs" :active-id="checks.activeRun.id" @select="checks.selectRun" />
      <OrderFilters :model-value="checks.filter" :search="checks.search" :orders="checks.orders" :visible-orders="checks.filteredOrders" :selected="checks.selected" @toggle-all="checks.toggleAllFiltered()" @update:model-value="checks.setFilter" @update:search="checks.search = $event" />
      <section v-if="checks.filteredOrders.length" class="order-grid">
        <OrderCard v-for="order in checks.filteredOrders" :key="order.order_id ?? order.id" :order="order" :reasons="checks.config?.return_reasons || []" :selected="checks.selected.includes(String(order.order_id ?? order.id))" @toggle="checks.toggle(order)" @decide="checks.decide(order, $event)" @return-comment="checks.setReturnComment(order, $event)" />
      </section>
      <section v-else class="empty-state surface"><div>⌁</div><h2>{{ checks.orders.length ? "Ничего не найдено" : "Заказы появятся здесь" }}</h2><p>{{ checks.orders.length ? "Измените фильтр или поисковый запрос." : "Первые карточки появятся ещё до завершения проверки." }}</p></section>
    </template>
    <section v-else-if="!checks.loading" class="first-run surface"><div class="first-icon">✦</div><p class="eyebrow">Можно начинать</p><h1>Проверьте первую папку с макетами</h1><p>Выберите папку и профиль печати. Имя файла должно содержать клиента и заказ, например <code>(12690-25506185)_offset-face.jpg</code>.</p><button class="button primary" @click="checks.drawerOpen = true">Начать первую проверку</button></section>
    <div v-else class="loading-page"><span class="spinner"></span> Загружаем рабочий пульт…</div>
  </main>
  <StartCheckPanel :open="checks.drawerOpen" :config="checks.config" :busy="checks.loading" @close="checks.drawerOpen = false" @start="checks.start" />
  <ActionBar :count="checks.selected.length" :selected-ids="checks.selected" :comments="checks.returnComments" :can-print="checks.canPrint" :busy="actionBusy" :reasons="checks.config?.return_reasons || []" @clear="checks.clearSelection" @action="act" />
  <div v-if="checks.conflictPrompt" class="confirm-backdrop">
    <section class="confirm-card">
      <h3>Файл уже существует</h3>
      <p>Найден конфликт для <strong>{{ checks.conflictPrompt.conflict?.destination_path }}</strong>. Можно заменить существующий файл новым или сохранить новый под другим именем.</p>
      <p v-if="checks.conflictPrompt.conflict?.source_path" class="muted">Источник: {{ checks.conflictPrompt.conflict.source_path }}</p>
      <p v-if="checks.conflictPrompt.conflict?.suggested_name" class="muted">Новый вариант имени: {{ checks.conflictPrompt.conflict.suggested_name }}</p>
      <div>
        <button class="button secondary" :disabled="actionBusy" @click="checks.conflictPrompt = null">Отмена</button>
        <button class="button danger" :disabled="actionBusy" @click="resolveConflict('replace')">Заменить существующий</button>
        <button class="button primary" :disabled="actionBusy" @click="resolveConflict('rename')">Сохранить под новым именем</button>
      </div>
    </section>
  </div>
  <button v-if="showBackToTop" type="button" class="back-to-top" title="В начало страницы" aria-label="В начало страницы" @click="backToTop">↑</button>
  <div v-if="toast" class="toast">{{ toast }}</div>
</template>
