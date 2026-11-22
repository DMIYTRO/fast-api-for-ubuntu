<script setup>
import { computed } from "vue";
import { useOrderFetchStore } from "../stores/orderFetch.js";
import { fetchProgressLabel, summarizeFetch } from "../services/orderFetch.js";

// Панель «Получить заказ»: прогресс идущей задачи и сводка завершённой.
// Кнопка запуска — в шапке рядом с «Новая проверка» (AppHeader), состояние общее (stores/orderFetch.js).
const orderFetch = useOrderFetchStore();
const summary = computed(() => summarizeFetch(orderFetch.snapshot));
const finished = computed(() => !orderFetch.running && ["done", "failed", "cancelled"].includes(orderFetch.snapshot?.state));
const label = computed(() => {
  if (orderFetch.running) return fetchProgressLabel(orderFetch.snapshot);
  if (orderFetch.starting) return "Запускаем получение заказов…";
  if (orderFetch.error) return orderFetch.error;
  if (finished.value) return summary.value.headline;
  return "";
});
const ok = computed(() => !orderFetch.error && finished.value && summary.value.ok);
const current = computed(() => (orderFetch.running ? (orderFetch.snapshot?.current || []).slice(0, 3).join(", ") : ""));
</script>

<template>
  <section v-if="orderFetch.visible" class="run-panel run-compact surface order-fetch" aria-label="Получение заказов">
    <div class="order-fetch-row">
      <div class="run-identity"><p class="eyebrow">Получение заказов</p></div>
      <div class="run-progress-compact" role="status" aria-live="polite">
        <span v-if="orderFetch.busy" class="run-throbber" aria-hidden="true"></span>
        <span v-else-if="ok" class="run-progress-marker" aria-hidden="true">✓</span>
        <span v-else class="run-waiting-marker" aria-hidden="true">!</span>
        <span class="run-progress-label">{{ label }}</span>
      </div>
      <button v-if="!orderFetch.busy" class="log-toggle" type="button" @click="orderFetch.dismiss()">Скрыть</button>
    </div>
    <p v-if="current" class="order-fetch-current muted" :title="current">В работе: {{ current }}</p>
    <p v-if="orderFetch.notice" class="order-fetch-notice">{{ orderFetch.notice }}</p>
    <template v-if="finished && !orderFetch.error">
      <p v-for="(issue, index) in summary.sourceErrors" :key="`e${index}`" class="order-fetch-source form-error" :title="issue.source">{{ issue.text }}</p>
      <p v-for="(issue, index) in summary.sourceWarnings" :key="`w${index}`" class="order-fetch-source order-fetch-warning muted" :title="issue.source">{{ issue.text }}</p>
      <details v-if="summary.problems.length" class="order-fetch-problems">
        <summary>Ошибки и замечания ({{ summary.problems.length }})</summary>
        <ul><li v-for="(problem, index) in summary.problems" :key="index">{{ problem }}</li></ul>
      </details>
    </template>
  </section>
</template>
