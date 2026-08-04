<script setup>
defineProps({ path: String, status: String, connection: String });
defineEmits(["new-check", "logout"]);
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
      <button class="button primary small" @click="$emit('new-check')">＋ Новая проверка</button>
      <button class="button ghost small" @click="$emit('logout')">Выйти</button>
    </div>
  </header>
</template>
