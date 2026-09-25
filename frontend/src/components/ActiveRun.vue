<script setup>
import { computed, ref } from "vue";
import { summarizeRunFileProgress } from "../stores/runFileProgress.js";
const props = defineProps({ run: Object, events: Array, connection: String, fileProgress: Object });
defineEmits(["cancel"]);
const showLog = ref(false);
const active = computed(() => ["queued", "running", "waiting_confirmation", "cancelling"].includes(props.run?.status));
const waitingConfirmation = computed(() => props.run?.status === "waiting_confirmation");
const processing = computed(() => ["queued", "running", "cancelling"].includes(props.run?.status));
const files = computed(() => summarizeRunFileProgress(props.fileProgress));
const progressLabel = computed(() => {
  if (waitingConfirmation.value) {
    if (files.value.finalized) {
      return `Ожидает подтверждения · обработано ${files.value.processedFiles} из ${files.value.totalFiles} файлов · осталось ${files.value.remainingFiles}`;
    }
    return "Ожидает подтверждения оператора";
  }
  if (!files.value.finalized) {
    if (!active.value) {
      if (props.run?.status === "failed") return "Проверка завершилась с ошибкой";
      if (props.run?.status === "cancelled") return "Проверка остановлена";
      return "Проверка завершена";
    }
    const found = files.value.discoveredFiles;
    return `Поиск и обработка · обработано ${files.value.processedFiles}, найдено ${found}; остаток уточняется`;
  }
  return `Обработано ${files.value.processedFiles} из ${files.value.totalFiles} файлов · осталось ${files.value.remainingFiles}`;
});
</script>

<template>
  <section v-if="run" class="run-panel run-compact surface">
    <div class="run-summary">
      <div class="run-identity">
        <p class="eyebrow">Текущая проверка</p>
        <div><strong>{{ run.stage_label || run.stage || "Подготовка" }}</strong><span class="muted">{{ run.current_order ? `Заказ ${run.current_order}` : run.input_path }}</span></div>
      </div>
      <div class="run-progress-compact" role="status" aria-live="polite">
        <span v-if="processing" class="run-throbber" aria-hidden="true"></span>
        <span v-else-if="waitingConfirmation" class="run-waiting-marker" aria-hidden="true">!</span>
        <span v-else class="run-progress-marker" aria-hidden="true">✓</span>
        <span class="run-progress-label">{{ progressLabel }}</span>
      </div>
      <div class="metrics metrics-compact">
        <div><b>{{ run.total_orders ?? 0 }}</b><span>найдено заказов</span></div>
        <div><b>{{ run.processed_orders ?? run.processed ?? 0 }}</b><span>проверено</span></div>
        <div><b class="green">{{ run.passed_orders ?? 0 }}</b><span>прошли</span></div>
        <div><b class="red">{{ run.failed_orders ?? run.problem_orders ?? 0 }}</b><span>проблемы</span></div>
      </div>
      <div class="run-actions">
        <button class="log-toggle" @click="showLog = !showLog">{{ showLog ? "Скрыть журнал" : "Журнал" }}</button>
        <nav class="export-links">
          <a v-if="run.html_url || run.report_url || run.report_ready" :href="run.html_url || run.report_url || `/runs/${run.id}/report`" target="_blank">HTML ↗</a>
          <a v-if="run.json_url" :href="run.json_url" target="_blank">JSON ↗</a>
          <a v-if="run.pdf_url" :href="run.pdf_url" target="_blank">PDF ↗</a>
        </nav>
        <button v-if="active" class="button danger button-small" @click="$emit('cancel')">Стоп</button>
      </div>
    </div>
    <ol v-if="showLog" class="event-log">
      <li v-for="(event, index) in events.slice(0, 30)" :key="event.event_id || index"><time>{{ event.created_at ? new Date(event.created_at).toLocaleTimeString("ru") : "сейчас" }}</time> {{ event.message || event.stage || event.type }}</li>
      <li v-if="!events.length">Событий пока нет</li>
    </ol>
  </section>
</template>
