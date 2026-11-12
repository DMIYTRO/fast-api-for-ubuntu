<script setup>
import { computed, nextTick, ref, useId } from "vue";
import PreviewImage from "./PreviewImage.vue";

const props = defineProps({
  comparison: { type: Object, required: true },
  busy: { type: Boolean, default: false },
});
const emit = defineEmits(["accept", "reject", "retry"]);
const zoomed = ref(null);
const noteId = `fix-distortion-${useId()}`;
const closeButton = ref(null);
let opener = null;

const status = computed(() => props.comparison.status || "processing");
const summary = computed(() => props.comparison.summary || {});
const planTitle = computed(() => props.comparison.plan?.title || props.comparison.plan?.key || "План не указан");
const hasResult = computed(() => ["ready", "accepted", "rejected"].includes(status.value));
const decided = computed(() => ["accepted", "rejected"].includes(status.value));
const tiles = computed(() => [
  { key: "original", label: "Оригинал", url: props.comparison.original_url },
  { key: "result", label: "Результат", url: props.comparison.result_url },
  { key: "overlay", label: "Наложение", url: props.comparison.overlay_url },
]);

const number = (value, maxDecimals) => {
  const parsed = Number(value);
  if (value == null || value === "" || !Number.isFinite(parsed)) return "";
  return String(Number(parsed.toFixed(maxDecimals))).replace(".", ",");
};
const scale = (value) => {
  const parsed = Number(value);
  return value == null || value === "" || !Number.isFinite(parsed) ? "" : parsed.toFixed(3).replace(".", ",");
};
const sizePair = (pair) => {
  if (!Array.isArray(pair) || pair.length < 2) return "";
  const [w, h] = [number(pair[0], 1), number(pair[1], 1)];
  return w && h ? `${w} × ${h}` : "";
};
const sizeText = computed(() => {
  const before = sizePair(summary.value.original_size_mm);
  const after = sizePair(summary.value.result_size_mm);
  if (before && after) return `Размер ${before} → ${after} мм`;
  if (after) return `Размер ${after} мм`;
  return "";
});
const changedText = computed(() => {
  const percent = number(summary.value.changed_percent, 1);
  return percent ? `Изменено ${percent} % площади` : "";
});
const distortionText = computed(() => {
  const x = scale(summary.value.scale_x);
  const y = scale(summary.value.scale_y);
  const axes = x && y ? ` (по ширине ×${x}, по высоте ×${y})` : "";
  return `Макет растянут неравномерно${axes}. Проверьте наложение перед принятием`;
});
const decisionText = computed(() => status.value === "accepted"
  ? "Исправление принято: результат станет новой ревизией печатного PDF."
  : "Исправление отклонено: в печать уходит исходный PDF.");
const statusText = computed(() => ({
  processing: "Обрабатывается",
  ready: "Ждёт решения",
  failed: "Ошибка",
  accepted: "Принято",
  rejected: "Отклонено",
}[status.value] || status.value));
const statusMark = computed(() => ({ processing: "…", ready: "?", failed: "✕", accepted: "✓", rejected: "—" }[status.value] || ""));

function open(tile, event) {
  if (!tile.url) return;
  opener = event?.currentTarget || null;
  zoomed.value = tile;
  nextTick(() => closeButton.value?.focus());
}
function close() {
  zoomed.value = null;
  nextTick(() => opener?.focus?.());
}
</script>

<template>
  <section class="fix-comparison" :class="`fix-${status}`" :aria-label="`Исправление Callas: ${planTitle}`" :aria-busy="status === 'processing' || busy">
    <header class="fix-head">
      <div>
        <p class="section-title">Исправление Callas · до и после</p>
        <strong>{{ planTitle }}</strong>
      </div>
      <span class="fix-status"><i aria-hidden="true">{{ statusMark }}</i>{{ statusText }}</span>
    </header>

    <p v-if="status === 'processing'" class="fix-note" role="status"><span class="spinner" aria-hidden="true"></span> Callas обрабатывает макет, это может занять несколько минут…</p>

    <div v-if="status === 'failed'" class="fix-error" role="alert">
      <p><b>Исправление не удалось.</b> {{ comparison.error || "Callas вернул ошибку без описания." }}</p>
      <button type="button" class="button secondary small" :disabled="busy" aria-label="Повторить исправление Callas" @click="emit('retry')">Повторить</button>
    </div>

    <template v-if="hasResult">
      <div class="fix-tiles">
        <figure v-for="tile in tiles" :key="tile.key" class="fix-tile" :class="`fix-tile-${tile.key}`">
          <figcaption>{{ tile.label }}</figcaption>
          <div
            v-if="tile.url"
            class="preview-image"
            role="button"
            tabindex="0"
            :aria-label="`Открыть крупно: ${tile.label}`"
            @click="open(tile, $event)"
            @keydown.enter.self.prevent="open(tile, $event)"
            @keydown.space.self.prevent="open(tile, $event)"
          >
            <PreviewImage :src="tile.url" :alt="`${tile.label}: ${planTitle}`" />
          </div>
          <div v-else class="preview-empty">Нет изображения</div>
        </figure>
      </div>

      <p v-if="sizeText || changedText" class="fix-summary">
        <span v-if="sizeText">{{ sizeText }}</span>
        <span v-if="changedText">{{ changedText }}</span>
      </p>

      <p v-if="summary.distortion" :id="noteId" class="fix-distortion" role="status"><i aria-hidden="true">ℹ</i><span><b>Масштаб по осям.</b> {{ distortionText }}</span></p>

      <p v-if="status === 'ready' && comparison.source_only" class="fix-note" role="status"><i aria-hidden="true">ℹ</i> Итогового PDF у заказа нет, поэтому принять исправление в печать отсюда нельзя. Скачайте исправленный PDF и проверьте заказ заново.</p>
      <a v-if="comparison.source_only && comparison.pdf_url && status === 'ready'" class="button secondary" :href="comparison.pdf_url" target="_blank" rel="noopener">Исправленный PDF ↗</a>
      <div v-else-if="status === 'ready'" class="fix-actions">
        <button type="button" class="button success" :disabled="busy" aria-label="Принять исправление Callas" :aria-describedby="summary.distortion ? noteId : null" @click="emit('accept')">✓ Принять</button>
        <button type="button" class="button secondary" :disabled="busy" aria-label="Отклонить исправление Callas" @click="emit('reject')">✕ Отклонить</button>
      </div>
      <p v-if="decided" class="fix-note fix-decision" role="status"><i aria-hidden="true">{{ statusMark }}</i> {{ decisionText }}</p>
    </template>
  </section>
  <Teleport to="body">
    <div v-if="zoomed" class="lightbox" role="dialog" aria-modal="true" :aria-label="`${zoomed.label}: крупный просмотр`" tabindex="-1" @click.self="close" @keydown.esc="close">
      <button ref="closeButton" type="button" class="icon-button" aria-label="Закрыть просмотр" @click="close">✕</button>
      <PreviewImage :src="zoomed.url" :alt="`${zoomed.label}: ${planTitle}`" :eager="true" />
    </div>
  </Teleport>
</template>
