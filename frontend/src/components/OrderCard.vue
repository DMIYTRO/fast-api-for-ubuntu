<script setup>
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { api } from "../services/api.js";
import PreviewViewer from "./PreviewViewer.vue";
import FileParameters from "./FileParameters.vue";
import CorrectionDecision from "./CorrectionDecision.vue";
import ReturnReasons from "./ReturnReasons.vue";
import PitStopReport from "./PitStopReport.vue";
import CallasFixControl from "./CallasFixControl.vue";
import { orderIssueGroups } from "./issueGroups.js";

const props = defineProps({ order: Object, runId: String, selected: Boolean, reasons: Array, paidDesign: { type: Boolean, default: true }, designCost: { type: String, default: "0" } });
const emit = defineEmits(["toggle", "decide", "return-comment", "return-design", "return-cost"]);
const expanded = ref(false);
const busy = ref(false);
const uploadInput = ref(null);
const uploadError = ref("");
const uploadingPreview = ref(false);
const layeredTiffExports = ref({});
const layeredTiffPolls = new Map();
const id = computed(() => props.order.order_id ?? props.order.id);
const actionId = computed(() => props.order.aggregate_id ?? id.value);
const files = computed(() => (props.order.files || props.order.file_results || []).map((file) => {
  const result = layeredTiffExports.value[file.id];
  if (!result) return file;
  return {
    ...file,
    ...(result.preview_url ? { preview_url: result.preview_url, thumbnail_url: result.preview_url, full_preview_url: result.preview_url } : {}),
    layered_tiff_export_pdf_url: result.pdf_url || "",
    layered_tiff_export_processing: result.status === "processing",
    layered_tiff_export_error: result.status === "failed" ? result.error : "",
  };
}));
// Заказ без итогового PDF (например, ошибка размера): исправлять можно его исходные PDF.
const sourceFixFiles = computed(() => (props.order.pdf_path || props.order.pdf_url ? [] : files.value.filter((file) => file.callas_fixable && file.callas_fix_file_id)));
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
// Локальные проблемы группируются по типу (order.issue_groups с бэкенда, иначе на месте);
// проблемы PitStop показывает PitStopReport.
const localIssueGroups = computed(() => orderIssueGroups({ ...props.order, files: files.value }).filter((group) => group.source !== "pitstop"));
const groupedErrors = computed(() => localIssueGroups.value.filter((group) => group.severity === "error"));
const groupedWarnings = computed(() => localIssueGroups.value.filter((group) => group.severity === "warning"));
const issueGroupHasDetails = (group) => group.count > 1 || (group.files || []).length > 0 || (group.pages || []).length > 0;
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
    const result = await api.uploadReturnPreview(props.runId, actionId.value, file);
    props.order.custom_preview_url = result.url;
  } catch (error) { uploadError.value = error.message; }
  finally { uploadingPreview.value = false; }
}
function onDrop(event) { uploadPreview(event.dataTransfer?.files?.[0]); }
function choosePreview() { uploadInput.value?.click(); }
function withLayeredTiffPreview(file, result) {
  if (result?.status === "completed" && result.pdf_url && !result.preview_url && file.layered_tiff_export_url) {
    return { ...result, preview_url: `${file.layered_tiff_export_url}/preview` };
  }
  return result;
}
function pollLayeredTiffExport(file) {
  clearTimeout(layeredTiffPolls.get(file.id));
  layeredTiffPolls.set(file.id, setTimeout(async () => {
    try {
      const result = await api.layeredTiffPreview(file.layered_tiff_export_url);
      layeredTiffExports.value[file.id] = withLayeredTiffPreview(file, result);
      if (result.status === "processing") pollLayeredTiffExport(file);
    } catch (error) {
      layeredTiffExports.value[file.id] = { status: "failed", error: error.message };
    }
  }, 900));
}
async function refreshLayeredTiffExport(file) {
  if (!file?.layered_tiff_export_url) return;
  try {
    const result = withLayeredTiffPreview(
      file,
      await api.layeredTiffPreview(file.layered_tiff_export_url),
    );
    layeredTiffExports.value[file.id] = result;
    if (result.status === "processing") pollLayeredTiffExport(file);
  } catch (error) {
    layeredTiffExports.value[file.id] = { status: "failed", error: error.message };
  }
}
async function retryLayeredTiffPreview(file) {
  if (!file.layered_tiff_export_url || layeredTiffExports.value[file.id]?.status === "processing") return;
  layeredTiffExports.value[file.id] = { status: "processing" };
  try {
    layeredTiffExports.value[file.id] = withLayeredTiffPreview(
      file,
      await api.layeredTiffPreview(file.layered_tiff_export_url, "POST"),
    );
    if (layeredTiffExports.value[file.id].status === "processing") pollLayeredTiffExport(file);
  } catch (error) {
    layeredTiffExports.value[file.id] = { status: "failed", error: error.message || "Не удалось создать PDF из TIFF." };
  }
}
watch(
  () => (props.order.action_result?.generated_layered_file_ids || []).join("|"),
  (generatedIds) => {
    if (!generatedIds) return;
    const ids = new Set(generatedIds.split("|"));
    (props.order.files || props.order.file_results || [])
      .filter((file) => [file.id, file.file_result_id, file.filename, file.name]
        .filter(Boolean)
        .some((identity) => ids.has(String(identity))))
      .forEach(refreshLayeredTiffExport);
  },
  { immediate: true },
);
onBeforeUnmount(() => layeredTiffPolls.forEach(clearTimeout));
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
        <PreviewViewer :files="files" :has-production-pdf="Boolean(order.pdf_path || order.pdf_url)" @retry-layered-tiff="retryLayeredTiffPreview" />
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
              <tr><th>Разрешение</th><td>{{ dpi(face) }}</td><td v-if="back">{{ dpi(back) }}</td><td>≥ 250 DPI</td></tr>
              <tr><th>Цветовая модель</th><td>{{ color(face) }}</td><td v-if="back">{{ color(back) }}</td><td>CMYK</td></tr>
              <tr><th>Формат файла</th><td>{{ format(face) }}</td><td v-if="back">{{ format(back) }}</td><td>JPG / JPEG / TIFF / PDF</td></tr>
              <tr><th>Автоповорот</th><td>{{ rotation(face) }}</td><td v-if="back">{{ rotation(back) }}</td><td>Минимальный</td></tr>
            </tbody>
          </table>
        </div>

        <div v-if="groupedErrors.length" class="issue-list errors">
          <strong>Ошибки проверки</strong>
          <ul>
            <li v-for="(item, index) in groupedErrors" :key="`group-${index}`" class="issue-group">
              <details v-if="issueGroupHasDetails(item)">
                <summary>{{ item.message }}<span v-if="item.count > 1" class="issue-group-count">×{{ item.count }}</span></summary>
                <div class="issue-group-details">
                  <p v-if="item.pages?.length">Страницы: {{ item.pages.join(", ") }}</p>
                  <p v-if="item.files?.length">Файлы: {{ item.files.join(", ") }}</p>
                  <ul v-if="item.samples?.length"><li v-for="(sample, sampleIndex) in item.samples" :key="`sample-${sampleIndex}`">{{ sample }}</li></ul>
                </div>
              </details>
              <template v-else>{{ item.message }}</template>
            </li>
          </ul>
        </div>
        <div v-if="order.status === 'error' && !order.pitstop && order.pitstop_status === 'not_checked'" class="pitstop-skipped">
          <strong>PitStop не проверял этот заказ</strong>
          <span>Причины обработки указаны выше. Отчёт PitStop появится после успешного создания итогового PDF.</span>
        </div>
        <div v-if="groupedWarnings.length" class="issue-list warnings">
          <strong>Предупреждения</strong>
          <ul>
            <li v-for="(item, index) in groupedWarnings" :key="`group-${index}`" class="issue-group">
              <details v-if="issueGroupHasDetails(item)">
                <summary>{{ item.message }}<span v-if="item.count > 1" class="issue-group-count">×{{ item.count }}</span></summary>
                <div class="issue-group-details">
                  <p v-if="item.pages?.length">Страницы: {{ item.pages.join(", ") }}</p>
                  <p v-if="item.files?.length">Файлы: {{ item.files.join(", ") }}</p>
                  <ul v-if="item.samples?.length"><li v-for="(sample, sampleIndex) in item.samples" :key="`sample-${sampleIndex}`">{{ sample }}</li></ul>
                </div>
              </details>
              <template v-else>{{ item.message }}</template>
            </li>
          </ul>
        </div>
        <div v-if="successful && !groupedErrors.length && !groupedWarnings.length" class="issue-list success-note"><strong>Заказ соответствует требованиям допечатной подготовки.</strong></div>
        <PitStopReport v-if="order.pitstop" :pitstop="order.pitstop" section-title="Проверка PDF из первоначальной обработки" />
        <PitStopReport v-if="order.action_result?.pitstop" :pitstop="order.action_result.pitstop" section-title="Повторная проверка печатного PDF из просмотренных TIFF" />
        <CallasFixControl v-if="order.pdf_path || order.pdf_url || order.callas_fix?.comparison" :run-id="runId" :order-id="actionId" :initial="order.callas_fix" :can-start="Boolean(order.pdf_path || order.pdf_url)" />
        <CallasFixControl
          v-for="file in sourceFixFiles"
          :key="`fix-${file.callas_fix_file_id}`"
          :run-id="runId"
          :order-id="actionId"
          :initial="file.callas_fix"
          :file-id="file.callas_fix_file_id"
          :file-label="sourceFixFiles.length > 1 ? file.filename : ''"
        />
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
