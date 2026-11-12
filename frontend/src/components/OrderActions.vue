<script setup>
import { ref } from "vue";
import { api } from "../services/api.js";
const props = defineProps({ runId: String, orderId: String });
const items = ref([]);
const error = ref("");
const loading = ref(false);
const statusLabel = (status) => ({ passed: "Готов", warning: "Требует доработки", error: "Ошибка", processing: "Выполняется", ready: "Готов к просмотру", failed: "Ошибка", accepted: "Принято", rejected: "Отклонено", accepted_for_print: "Передан в печать", returned_for_rework: "Возвращён на доработку" })[status] || status;
async function load(event) {
  if (!event.target.open || loading.value) return;
  loading.value = true;
  error.value = "";
  try {
    items.value = (await api.orderActions(props.runId, props.orderId)).items || [];
  } catch (cause) {
    error.value = cause.message || "Не удалось загрузить действия заказа";
  } finally { loading.value = false; }
}
</script>
<template>
  <details class="order-actions" @toggle="load">
    <summary>Действия с заказом</summary>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="loading">Загрузка…</p>
    <ol v-else-if="items.length">
      <li v-for="item in items" :key="item.id">
        <time>{{ item.created_at }}</time> — {{ item.label }}<span v-if="item.status"> · {{ statusLabel(item.status) }}</span>
        <span v-if="item.pdf_revision"> · PDF r{{ item.pdf_revision }}</span>
        <span v-if="item.details?.plan"> · {{ item.details.plan }}</span>
        <span v-for="(operation, index) in item.details?.operations || []" :key="index"> · {{ operation.kind === 'size_correction' ? 'Размер скорректирован' : 'ICC-профиль удалён' }}: {{ operation.file }}</span>
        <span v-for="file in item.details?.tiff_flattened || []" :key="file.file"> · Сведены слои TIFF: {{ file.file }}</span>
      </li>
    </ol>
    <p v-else>Записанных действий пока нет.</p>
  </details>
</template>
