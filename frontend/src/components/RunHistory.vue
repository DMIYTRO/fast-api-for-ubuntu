<script setup>
defineProps({ runs: Array, activeId: [String, Number] });
defineEmits(["select"]);
const status = { queued: "В очереди", running: "Выполняется", completed: "Готово", failed: "Ошибка", cancelled: "Отменено" };
const date = (value) => value ? new Date(value).toLocaleString("ru", { dateStyle: "short", timeStyle: "short" }) : "—";
</script>

<template>
  <details class="history surface">
    <summary>История проверок <span>{{ runs.length }}</span></summary>
    <div class="history-list">
      <button v-for="run in runs" :key="run.id" :class="{ active: String(activeId) === String(run.id) }" @click="$emit('select', run.id)">
        <span><strong>{{ run.input_path?.split(/[\\/]/).pop() || `Проверка ${run.id}` }}</strong><small>{{ date(run.created_at || run.started_at) }}</small></span>
        <em :class="`status-${run.status}`">{{ status[run.status] || run.status }}</em>
      </button>
    </div>
  </details>
</template>
