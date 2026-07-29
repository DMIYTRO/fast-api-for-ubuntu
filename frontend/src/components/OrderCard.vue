<script setup>
import { computed, ref } from "vue";
import PreviewViewer from "./PreviewViewer.vue";
import FileParameters from "./FileParameters.vue";
import CorrectionDecision from "./CorrectionDecision.vue";
import ReturnReasons from "./ReturnReasons.vue";

const props = defineProps({ order: Object, selected: Boolean, reasons: Array });
const emit = defineEmits(["toggle", "decide", "return-comment"]);
const expanded = ref(false);
const busy = ref(false);
const id = computed(() => props.order.order_id ?? props.order.id);
const files = computed(() => props.order.files || props.order.file_results || []);
const face = computed(() => files.value.find((file) => String(file.side || file.parsed?.side).toLowerCase() === "face") || files.value[0]);
const back = computed(() => files.value.find((file) => String(file.side || file.parsed?.side).toLowerCase() === "back"));
const parsed = computed(() => face.value?.parsed || {});
const colorMode = computed(() => parsed.value.front_colors != null ? `${parsed.value.front_colors}-${parsed.value.back_colors ?? 0}` : "—");
const declaredSize = computed(() => parsed.value.width_mm && parsed.value.height_mm ? `${parsed.value.width_mm} × ${parsed.value.height_mm} мм` : "Размер не указан");
const formatNumber = (input, decimals = 1) => {
  const number = Number(input);
  return Number.isFinite(number) ? number.toFixed(decimals) : "—";
};
const formatMm = (input) => formatNumber(input, 1);
const expectedSize = computed(() => parsed.value.width_mm && parsed.value.height_mm ? `${formatMm(parsed.value.width_mm + 4)} × ${formatMm(parsed.value.height_mm + 4)} мм` : "—");
const issueText = (value) => typeof value === "string" ? value : value?.message || value?.code;
const allErrors = computed(() => [
  ...(props.order.errors || []),
  ...files.value.flatMap((file) => file.errors || []),
].map(issueText).filter(Boolean));
const allWarnings = computed(() => [
  ...(props.order.warnings || []),
  ...files.value.flatMap((file) => file.warnings || []),
].map(issueText).filter(Boolean));
const statusLabel = { detected: "Обнаружен", passed: "Прошёл", completed: "Прошёл", warning: "Предупреждение", error: "Ошибка", failed: "Ошибка", waiting_confirmation: "Нужно решение", processing: "Проверяется" };
const value = (input, suffix = "") => input != null && input !== "" ? `${input}${suffix}` : "—";
const size = (file) => file ? `${formatMm(file.width_mm ?? file.actual_width_mm)} × ${formatMm(file.height_mm ?? file.actual_height_mm)} мм` : "—";
const dpi = (file) => {
  if (!file) return "—";
  if (file.pdf_content_type === "vector") return "Вектор";
  const x = file.dpi_x ?? file.pdf_min_dpi ?? file.dpi;
  const y = file.dpi_y ?? file.pdf_min_dpi ?? file.dpi;
  return x != null || y != null
    ? `${formatNumber(x)} × ${formatNumber(y)} DPI`
    : "Не определяется";
};
const color = (file) => file?.colorspace || file?.color_space || "—";
const format = (file) => file?.format || file?.actual_format || "—";
const rotation = (file) => file ? `${file.rotation_degrees || 0}°` : "—";

async function decide(value) {
  busy.value = true;
  try { await emit("decide", value); } finally { busy.value = false; }
}
</script>

<template>
  <article class="order-card order-card-wide" :class="[`order-${order.status}`, { selected }]">
    <header class="order-head">
      <label class="select-order"><input type="checkbox" :checked="selected" @change="$emit('toggle')"><span></span></label>
      <div><p class="eyebrow">Заказ</p><h3>№ {{ id }}</h3><small>Клиент {{ order.customer_id || "—" }}</small></div>
      <div class="order-meta">
        <span>Красочность <b>{{ colorMode }}</b></span>
        <span>Макет <b>{{ declaredSize }}</b></span>
        <span>Файлов <b>{{ files.length }}</b></span>
      </div>
      <div class="order-head-actions">
        <ReturnReasons :order="order" :reasons="reasons" @change="$emit('return-comment', $event)" />
        <span class="order-status" :class="`status-${order.status}`">{{ statusLabel[order.status] || order.status }}</span>
      </div>
    </header>

    <div class="order-body">
      <section class="preview-column">
        <p class="section-title">Превью макетов</p>
        <PreviewViewer :files="files" />
        <div class="frame-legend">
          <span><i class="legend-green"></i>Зелёный — край реза</span>
          <span><i class="legend-red"></i>Красный — безопасная зона</span>
        </div>
      </section>

      <section class="inspection-column">
        <p class="section-title">Параметры проверки</p>
        <div class="specs-scroll">
          <table class="specs-table">
            <thead><tr><th>Параметр</th><th>Face</th><th v-if="back">Back</th><th>Норма</th></tr></thead>
            <tbody>
              <tr><th>Размер с вылетами</th><td>{{ size(face) }}</td><td v-if="back">{{ size(back) }}</td><td>{{ expectedSize }}</td></tr>
              <tr><th>Разрешение</th><td>{{ dpi(face) }}</td><td v-if="back">{{ dpi(back) }}</td><td>≥ 300 DPI</td></tr>
              <tr><th>Цветовая модель</th><td>{{ color(face) }}</td><td v-if="back">{{ color(back) }}</td><td>CMYK</td></tr>
              <tr><th>Формат файла</th><td>{{ format(face) }}</td><td v-if="back">{{ format(back) }}</td><td>JPG / JPEG / TIFF / PDF</td></tr>
              <tr><th>Автоповорот</th><td>{{ rotation(face) }}</td><td v-if="back">{{ rotation(back) }}</td><td>Минимальный</td></tr>
            </tbody>
          </table>
        </div>

        <div v-if="allErrors.length" class="issue-list errors"><strong>Ошибки проверки</strong><ul><li v-for="item in allErrors" :key="item">{{ item }}</li></ul></div>
        <div v-if="allWarnings.length" class="issue-list warnings"><strong>Предупреждения</strong><ul><li v-for="item in allWarnings" :key="item">{{ item }}</li></ul></div>
        <div v-if="order.passed && !allErrors.length && !allWarnings.length" class="issue-list success-note"><strong>Заказ соответствует требованиям допечатной подготовки.</strong></div>
        <CorrectionDecision :order="order" :busy="busy" @decide="decide" />
        <p v-if="order.action_result" class="action-result" :class="`status-${order.action_result.status}`">Действие: {{ order.action_result.status === "prepared" ? "подготовлено" : (order.action_result.message || order.action_result.status) }}</p>
      </section>
    </div>

    <button v-if="files.length" class="details-toggle" @click="expanded = !expanded">{{ expanded ? "Скрыть исходные файлы" : "Исходные файлы и подробности" }} <span>{{ expanded ? "⌃" : "⌄" }}</span></button>
    <div v-if="expanded" class="parameters-list"><FileParameters v-for="file in files" :key="file.id || file.filename" :file="file" /></div>
    <footer class="order-footer">
      <span class="muted">Причины возврата настраиваются кнопкой 💬 в заголовке</span>
      <nav class="export-links">
        <a v-if="order.pdf_url || order.pdf_path" :href="order.pdf_url || `/api/orders/${id}/pdf`" target="_blank">PDF ↗</a>
        <a v-if="order.html_url" :href="order.html_url" target="_blank">HTML ↗</a>
        <a v-if="order.json_url" :href="order.json_url" target="_blank">JSON ↗</a>
      </nav>
    </footer>
  </article>
</template>
