<script setup>
// showFetch/fetchBusy — кнопка «Получить заказ» (только на рабочем пульте, см. OrderFetch.vue).
defineProps({ path: String, status: String, connection: String, showFetch: Boolean, fetchBusy: Boolean });
defineEmits(["new-check", "fetch-orders", "logout"]);
import { theme, toggleTheme } from "../theme.js";

const labels = { queued: "В очереди", running: "Проверка идёт", waiting_confirmation: "Нужно решение", completed: "Готово", failed: "Ошибка", cancelled: "Отменено" };
</script>

<template>
  <header class="app-header">
    <div class="brand">
      <span class="brand-symbol" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="square" stroke-linejoin="miter">
          <path d="M6 9V3h12v6" />
          <path d="M6 18H4V10h16v8h-2" />
          <path d="M6 14h12v7H6z" />
          <path d="M17 12h.01" />
        </svg>
      </span>
      <span>PrePress Flow</span>
    </div>
    <div class="header-context">
      <div class="path-label"><small>Рабочая папка</small><strong :title="path">{{ path || "Не выбрана" }}</strong></div>
      <span class="status-pill" :class="`status-${status || 'idle'}`">
        <i :class="{ pulse: status === 'running' }"></i>{{ labels[status] || "Ожидание" }}
      </span>
      <span v-if="connection === 'reconnecting'" class="connection">Восстанавливаем соединение…</span>
    </div>
    <div class="header-actions">
      <RouterLink class="button ghost small" to="/history">История</RouterLink>
      <button v-if="showFetch" class="button ghost small order-fetch-button" type="button" :disabled="fetchBusy" :aria-busy="fetchBusy" title="Получить файлы в рабочую папку и запустить проверку" @click="$emit('fetch-orders')">Получить заказ</button>
      <button class="button primary small" @click="$emit('new-check')">＋ Новая проверка</button>
      <button class="button ghost small theme-toggle" type="button" :aria-pressed="theme === 'dark'" :title="theme === 'dark' ? 'Включить светлую тему' : 'Включить тёмную тему'" @click="toggleTheme">
        <svg v-if="theme === 'dark'" viewBox="0 0 20 20" aria-hidden="true" focusable="false"><circle cx="10" cy="10" r="3.4" /><path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4" /></svg>
        <svg v-else viewBox="0 0 20 20" aria-hidden="true" focusable="false"><path d="M16.5 11.6A6.6 6.6 0 0 1 8.4 3.5a6.6 6.6 0 1 0 8.1 8.1Z" /></svg>
        <span class="visually-hidden">{{ theme === "dark" ? "Светлая тема" : "Тёмная тема" }}</span>
      </button>
      <button class="button ghost small" @click="$emit('logout')">Выйти</button>
    </div>
  </header>
</template>
