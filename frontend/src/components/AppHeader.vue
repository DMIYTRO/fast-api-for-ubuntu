<script setup>
defineProps({ path: String, status: String, connection: String });
defineEmits(["new-check", "logout"]);
const labels = { queued: "В очереди", running: "Проверка идёт", waiting_confirmation: "Нужно решение", completed: "Готово", failed: "Ошибка", cancelled: "Отменено" };
</script>

<template>
  <header class="app-header">
    <div class="brand"><span class="brand-symbol">✦</span><span>Image Magic</span></div>
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
