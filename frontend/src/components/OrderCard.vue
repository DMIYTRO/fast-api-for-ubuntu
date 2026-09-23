<script setup>
import { computed, ref } from "vue";
import { api } from "../services/api.js";
import PreviewViewer from "./PreviewViewer.vue";
import FileParameters from "./FileParameters.vue";
import CorrectionDecision from "./CorrectionDecision.vue";
import ReturnReasons from "./ReturnReasons.vue";
import PitStopReport from "./PitStopReport.vue";

const props = defineProps({ order: Object, runId: String, selected: Boolean, reasons: Array, paidDesign: { type: Boolean, default: true }, designCost: { type: String, default: "0" } });
const emit = defineEmits(["toggle", "decide", "return-comment", "return-design", "return-cost"]);
const expanded = ref(false);
const busy = ref(false);
const uploadInput = ref(null);
const uploadError = ref("");
const uploadingPreview = ref(false);
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
const statusLabel = { detected: "Обнаружен", passed: "Прошёл", completed: "Прошёл", warning: "Предупреждение", error: "Ошибка PDF", failed: "Ошибка", technical_error: "Сбой проверки", pitstop_checking: "PitStop проверяет", waiting_confirmation: "Нужно решение", processing: "Проверяется" };
const successful = computed(() => {
  if (!["passed", "completed"].includes(props.order.status)) return false;
  if (!props.order.pitstop) return true;
  const execution = String(props.order.pitstop.execution_status || "").toLowerCase();
  const verdict = String(props.order.pitstop.verdict || "").toLowerCase();
  return execution === "completed" && ["passed", "ok", "success"].includes(verdict);
});
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
const fold = computed(() => props.order?.postpress?.fold || null);
const foldCount = computed(() => {
  const count = Number(fold.value?.count);
  return Number.isFinite(count) && count > 0 ? count : "?";
});
const foldLabel = computed(() => {
  if (!fold.value) return "";
  const labels = {
    "half-fold": "пополам",
    "c-fold": "намотка",
    "z-fold": "гармошка",
  };
  const operation = fold.value.operation || "Сгиб";
  return fold.value.label || `${operation}: ${labels[fold.value.type] || "схема"} · ${foldCount.value}`;
});
const foldSupported = computed(() => {
  if (!fold.value) return false;
  if (typeof fold.value.supported === "boolean") return fold.value.supported;
  return ["half-fold", "c-fold", "z-fold"].includes(fold.value.type);
});
const foldTitle = computed(() => foldSupported.value ? foldLabel.value : `${foldLabel.value}. Схема фальцовки требует настройки`);

async function decide(value) {
  busy.value = true;
  try { await emit("decide", value); } finally { busy.value = false; }
}
async function uploadPreview(file) {
  uploadError.value = "";
  if (!file) return;
  if (!["image/jpeg", "image/png"].includes(file.type)) {
    uploadError.value = "Можно загрузить только JPEG или PNG.";
    return;
  }
  if (file.size > 1024 * 1024) {
    uploadError.value = "Размер превью не должен превышать 1 МБ.";
    return;
  }
  uploadingPreview.value = true;
  try {
    const result = await api.uploadReturnPreview(props.runId, id.value, file);
    props.order.custom_preview_url = result.url;
  } catch (error) { uploadError.value = error.message; }
  finally { uploadingPreview.value = false; }
}
function onDrop(event) { uploadPreview(event.dataTransfer?.files?.[0]); }
function choosePreview() { uploadInput.value?.click(); }
</script>

<template>
  <article class="order-card order-card-wide" :class="[`order-${order.status}`, { selected }]">
    <header class="order-head">
      <label class="select-order"><input type="checkbox" :checked="selected" @change="$emit('toggle')"><span></span></label>
      <div>
        <p class="eyebrow">Заказ</p><h3>№ {{ id }}</h3>
        <small class="customer-line">
          <span>Клиент {{ order.customer_id || "—" }}</span>
          <span v-if="fold" class="fold-indicator" :class="{ unsupported: !foldSupported }" :title="foldTitle" :aria-label="foldTitle">
            <svg viewBox="0 0 20 20" aria-hidden="true"><path d="M3 3v14m14-14v14M3 5h14M3 15h14M10 3v14M7.5 8.5 10 11l2.5-2.5" /></svg>
            <b>{{ foldLabel }}</b>
            <i v-if="!foldSupported" aria-hidden="true">!</i>
          </span>
        </small>
      </div>
      <div class="order-meta">
        <span>Красочность <b>{{ colorMode }}</b></span>
        <span>Макет <b>{{ declaredSize }}</b></span>
        <span>Файлов <b>{{ files.length }}</b></span>
      </div>
      <div class="order-head-actions">
        <ReturnReasons :order="order" :reasons="reasons" @change="$emit('return-comment', $event)" />
        <button type="button" class="paid-design" :class="{ active: paidDesign }" :aria-pressed="paidDesign" title="Предложить клиенту платную доработку" @click.stop="$emit('return-design', !paidDesign)">
          Платная доработка
        </button>
        <label v-if="paidDesign" class="design-cost">
          <span>Стоимость</span>
          <input :value="designCost" type="number" min="0" step="0.01" inputmode="decimal" aria-label="Стоимость платной доработки" @input="$emit('return-cost', $event.target.value)">
        </label>
        <span class="order-status" :class="`status-${order.status}`">{{ statusLabel[order.status] || order.status }}</span>
      </div>
    </header>

    <div class="order-body">
      <section class="preview-column">
        <p class="section-title">Превью макетов</p>
        <PreviewViewer :files="files" />
        <div class="custom-preview" @dragover.prevent @drop.prevent="onDrop">
          <input ref="uploadInput" type="file" accept="image/jpeg,image/png" hidden @change="uploadPreview($event.target.files?.[0])">
          <button type="button" class="button secondary small" :disabled="uploadingPreview" @click="choosePreview">{{ uploadingPreview ? "Загрузка…" : "Загрузить превью для возврата" }}</button>
          <span>JPEG или PNG, до 1 МБ. Можно перетащить файл сюда.</span>
          <a v-if="order.custom_preview_url" :href="order.custom_preview_url" target="_blank">Пользовательское превью выбрано для FTP ↗</a>
          <small v-if="uploadError" class="form-error">{{ uploadError }}</small>
        </div>
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
              <tr><th>Разрешение</th><td>{{ dpi(face) }}</td><td v-if="back">{{ dpi(back) }}</td><td>≥ 270 DPI</td></tr>
              <tr><th>Цветовая модель</th><td>{{ color(face) }}</td><td v-if="back">{{ color(back) }}</td><td>CMYK</td></tr>
              <tr><th>Формат файла</th><td>{{ format(face) }}</td><td v-if="back">{{ format(back) }}</td><td>JPG / JPEG / TIFF / PDF</td></tr>
              <tr><th>Автоповорот</th><td>{{ rotation(face) }}</td><td v-if="back">{{ rotation(back) }}</td><td>Минимальный</td></tr>
            </tbody>
          </table>
        </div>

        <div v-if="allErrors.length" class="issue-list errors"><strong>Ошибки проверки</strong><ul><li v-for="item in allErrors" :key="item">{{ item }}</li></ul></div>
        <div v-if="allWarnings.length" class="issue-list warnings"><strong>Предупреждения</strong><ul><li v-for="item in allWarnings" :key="item">{{ item }}</li></ul></div>
        <div v-if="successful && !allErrors.length && !allWarnings.length" class="issue-list success-note"><strong>Заказ соответствует требованиям допечатной подготовки.</strong></div>
        <PitStopReport v-if="order.pitstop" :pitstop="order.pitstop" />
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
