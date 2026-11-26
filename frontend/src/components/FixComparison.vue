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
const TILE_KINDS = [
  ["original", "Оригинал"],
  ["result", "Результат"],
  ["overlay", "Наложение"],
];
const tilesOf = (source, side = "") => TILE_KINDS.map(([key, label]) => ({
  key,
  label,
  zoomLabel: side ? `${side} · ${label}` : label,
  url: source?.[`${key}_url`] || null,
}));
// Страницы сравнения от бэкенда (лицо и оборот); старые ответы без них — одна страница.
const comparedPages = computed(() => (Array.isArray(props.comparison.pages) ? props.comparison.pages : [])
  .filter((page) => page && Number.isInteger(page.page) && (page.original_url || page.result_url || page.overlay_url)));
const pageCount = computed(() => {
  const count = Number(summary.value.original_page_count);
  return Number.isInteger(count) && count > 0 ? count : comparedPages.value.length;
});
const sideLabel = (number) => {
  if (pageCount.value <= 2) return number === 1 ? "Лицо" : "Оборот";
  return `Стр. ${number}`;
};
const rows = computed(() => {
  if (comparedPages.value.length < 2) return [{ key: "page-1", side: "", changed: "", tiles: tilesOf(props.comparison) }];
  return comparedPages.value.map((page) => {
    const side = sideLabel(page.page);
    const percent = number(page.changed_percent, 1);
    return { key: `page-${page.page}`, side, changed: percent ? `изменено ${percent} %` : "", tiles: tilesOf(page, side) };
  });
});
const multiPage = computed(() => rows.value.length > 1);

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
  if (multiPage.value) return "";
  const percent = number(summary.value.changed_percent, 1);
  return percent ? `Изменено ${percent} % площади` : "";
});
const distortionText = computed(() => {
  const x = scale(summary.value.scale_x);
  const y = scale(summary.value.scale_y);
  const axes = x && y ? ` (по ширине ×${x}, по высоте ×${y})` : "";
  return `Макет растянут неравномерно${axes}. Проверьте наложение перед принятием`;
});
// Исправление структурное (шрифты в кривые, обтравка…): картинка не изменилась, наложение пустое — это норма.
// Учитываются все сравнённые страницы: изменение может быть только на обороте.
const unchanged = (page) => {
  const overlay = Number(page?.overlay_changed_percent);
  return Number.isFinite(overlay) && overlay === 0 && !(Number(page?.changed_percent) > 0);
};
const noVisibleChange = computed(() => unchanged(summary.value)
  && comparedPages.value.filter((page) => page.page > 1).every(unchanged));
// Исходный PDF заказа без собранного PDF: принять можно, только когда бэкенд это разрешил
// (заказ ждёт решения по коррекции), иначе результат только для просмотра и скачивания.
const sourceOnly = computed(() => Boolean(props.comparison.source_only));
const canDecide = computed(() => status.value === "ready" && (!sourceOnly.value || Boolean(props.comparison.can_accept)));
const previewOnly = computed(() => status.value === "ready" && sourceOnly.value && !props.comparison.can_accept);
const decisionText = computed(() => {
  if (sourceOnly.value) {
    return status.value === "accepted"
      ? "Принято: этот файл будет собран из исправленного PDF. Исходный файл не изменён."
      : "Исправление отклонено: файл проверяется по исходному файлу.";
  }
  return status.value === "accepted"
    ? "Исправление принято: результат станет новой ревизией печатного PDF."
    : "Исправление отклонено: в печать уходит исходный PDF.";
});
// Только для коррекции размера: бэкенд сообщает, изменились ли только поля (вылеты) или макет внутри обреза.
// Старые ответы без поля: ничего не показываем.
const bleedOnly = computed(() => {
  if (props.comparison.plan?.key !== "size_correction") return null;
  const value = summary.value.bleed_only;
  return typeof value === "boolean" ? value : null;
});
const bleedReasons = computed(() => (Array.isArray(summary.value.bleed_only_reasons) ? summary.value.bleed_only_reasons.filter(Boolean) : []));
// Профиль callas ничего не изменил (например, растр уже уменьшен до 300 ppi при сборке):
// исправлять было нечего — это не сбой, повторять незачем.
const noChange = computed(() => status.value === "failed" && props.comparison.error_code === "callas_no_change");
const statusText = computed(() => noChange.value ? "Без изменений" : ({
  processing: "Обрабатывается",
  ready: "Ждёт решения",
  failed: "Ошибка",
  accepted: "Принято",
  rejected: "Отклонено",
}[status.value] || status.value));
const statusMark = computed(() => noChange.value ? "—" : ({ processing: "…", ready: "?", failed: "✕", accepted: "✓", rejected: "—" }[status.value] || ""));

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
  <section class="fix-comparison" :class="noChange ? 'fix-no-change-state' : `fix-${status}`" :aria-label="`Исправление Callas: ${planTitle}`" :aria-busy="status === 'processing' || busy">
    <header class="fix-head">
      <div>
        <p class="section-title">Исправление Callas · до и после</p>
        <strong>{{ planTitle }}</strong>
      </div>
      <span class="fix-status"><i aria-hidden="true">{{ statusMark }}</i>{{ statusText }}</span>
    </header>

    <p v-if="status === 'processing'" class="fix-note" role="status"><span class="spinner" aria-hidden="true"></span> Callas обрабатывает макет, это может занять несколько минут…</p>

    <p v-if="noChange" class="fix-no-change" role="status"><b>Менять нечего.</b> {{ comparison.error }}</p>
    <div v-else-if="status === 'failed'" class="fix-error" role="alert">
      <p><b>Исправление не удалось.</b> {{ comparison.error || "Callas вернул ошибку без описания." }}</p>
      <button type="button" class="button secondary small" :disabled="busy" aria-label="Повторить исправление Callas" @click="emit('retry')">Повторить</button>
    </div>

    <template v-if="hasResult">
      <div v-for="row in rows" :key="row.key" class="fix-page" :class="{ 'fix-page-multi': multiPage }" :role="multiPage ? 'group' : null" :aria-label="multiPage ? row.side : null">
        <p v-if="multiPage" class="fix-page-title"><b>{{ row.side }}</b><span v-if="row.changed" class="muted">{{ row.changed }}</span></p>
        <div class="fix-tiles">
          <figure v-for="tile in row.tiles" :key="tile.key" class="fix-tile" :class="`fix-tile-${tile.key}`">
            <figcaption>{{ tile.label }}</figcaption>
            <div
              v-if="tile.url"
              class="preview-image"
              role="button"
              tabindex="0"
              :aria-label="`Открыть крупно: ${tile.zoomLabel}`"
              @click="open(tile, $event)"
              @keydown.enter.self.prevent="open(tile, $event)"
              @keydown.space.self.prevent="open(tile, $event)"
            >
              <PreviewImage :src="tile.url" :alt="`${tile.zoomLabel}: ${planTitle}`" />
            </div>
            <div v-else class="preview-empty">Нет изображения</div>
          </figure>
        </div>
      </div>

      <p class="fix-legend muted" aria-label="Обозначения наложения">
        Наложение: <span class="legend-removed">красный — стало светлее</span> · <span class="legend-added">синий — стало темнее</span> · <span class="legend-recolored">пурпурный — сменился цвет</span>
      </p>
      <p v-if="noVisibleChange" class="fix-note" role="status"><i aria-hidden="true">ℹ</i> Внешний вид макета не изменился: исправление затрагивает структуру PDF (шрифты, обтравка, цветовая модель), поэтому наложение пустое.</p>

      <p v-if="sizeText || changedText" class="fix-summary">
        <span v-if="sizeText">{{ sizeText }}</span>
        <span v-if="changedText">{{ changedText }}</span>
      </p>

      <p v-if="bleedOnly === true" class="fix-note fix-bleed-only" role="status"><i aria-hidden="true">✓</i> Только поля: макет внутри обрезного формата не изменён</p>
      <div v-else-if="bleedOnly === false" class="fix-plan-warning fix-bleed-changed" role="status">
        <i aria-hidden="true">!</i>
        <span>Фикс изменил макет, не только поля:<template v-if="bleedReasons.length"><ul class="fix-bleed-reasons"><li v-for="(reason, index) in bleedReasons" :key="index">{{ reason }}</li></ul></template></span>
      </div>
      <p v-if="summary.plan_warning" class="fix-plan-warning" role="note"><i aria-hidden="true">!</i><span>{{ summary.plan_warning }}</span></p>
      <p v-if="summary.partial_failure" class="fix-plan-warning fix-partial" role="alert"><i aria-hidden="true">!</i><span>{{ summary.partial_failure }}</span></p>
      <ul v-if="summary.post_check?.messages?.length" class="fix-post-check" :class="`post-${summary.post_check.level}`" aria-label="Повторная проверка результата">
        <li v-for="(message, index) in summary.post_check.messages" :key="index">{{ message }}</li>
      </ul>

      <p v-if="summary.distortion" :id="noteId" class="fix-distortion" role="status"><i aria-hidden="true">ℹ</i><span><b>Масштаб по осям.</b> {{ distortionText }}</span></p>

      <p v-if="previewOnly" class="fix-note" role="status"><i aria-hidden="true">ℹ</i> Итогового PDF у заказа нет, поэтому принять исправление в печать отсюда нельзя. Скачайте исправленный PDF и проверьте заказ заново.</p>
      <p v-else-if="canDecide && sourceOnly" class="fix-note" role="status"><i aria-hidden="true">ℹ</i> Если принять исправление, этот файл будет собран из исправленного PDF, исходный файл не изменится. Когда исправления приняты для всех файлов, ждущих решения, заказ продолжит проверку сам.</p>
      <a v-if="previewOnly && comparison.pdf_url" class="button secondary" :href="comparison.pdf_url" target="_blank" rel="noopener">Исправленный PDF ↗</a>
      <div v-else-if="canDecide" class="fix-actions">
        <button type="button" class="button success" :disabled="busy" aria-label="Принять исправление Callas" :aria-describedby="summary.distortion ? noteId : null" @click="emit('accept')">✓ Принять</button>
        <button type="button" class="button secondary" :disabled="busy" aria-label="Отклонить исправление Callas" @click="emit('reject')">✕ Отклонить</button>
      </div>
      <p v-if="decided" class="fix-note fix-decision" role="status"><i aria-hidden="true">{{ statusMark }}</i> {{ decisionText }}</p>
    </template>
  </section>
  <Teleport to="body">
    <div v-if="zoomed" class="lightbox" role="dialog" aria-modal="true" :aria-label="`${zoomed.zoomLabel}: крупный просмотр`" tabindex="-1" @click.self="close" @keydown.esc="close">
      <button ref="closeButton" type="button" class="icon-button" aria-label="Закрыть просмотр" @click="close">✕</button>
      <PreviewImage :src="zoomed.url" :alt="`${zoomed.zoomLabel}: ${planTitle}`" :eager="true" />
    </div>
  </Teleport>
</template>
