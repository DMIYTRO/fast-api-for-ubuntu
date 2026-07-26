<script setup>
import { computed, ref } from "vue";
const props = defineProps({ run: Object, events: Array, connection: String });
defineEmits(["cancel"]);
const showLog = ref(false);
const progress = computed(() => Math.max(0, Math.min(100, Number(props.run?.progress) || 0)));
const active = computed(() => ["queued", "running", "waiting_confirmation", "cancelling"].includes(props.run?.status));
</script>

<template>
  <section v-if="run" class="run-panel run-compact surface">
    <div class="run-summary">
      <div class="run-identity">
        <p class="eyebrow">Текущая проверка</p>
        <div><strong>{{ run.stage || "Подготовка" }}</strong><span class="muted">{{ run.current_order ? `Заказ ${run.current_order}` : run.input_path }}</span></div>
      </div>
      <div class="run-progress-compact">
        <div class="progress-track"><i :style="{ width: `${progress}%` }"></i></div>
        <strong>{{ progress }}%</strong>
      </div>
      <div class="metrics metrics-compact">
        <div><b>{{ run.total_orders ?? 0 }}</b><span>найдено</span></div>
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
