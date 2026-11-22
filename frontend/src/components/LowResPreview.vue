<script setup>
import { computed, ref } from "vue";
import { api } from "../services/api.js";
import PreviewImage from "./PreviewImage.vue";
import { lowResolutionGroup } from "./prepress.js";

// «Показать разрешение»: callas подписывает ppi каждого цветного/полутонового изображения PDF
// заказа (services/lowres_preview.py). Строится только по нажатию; повтор при том же PDF — из кэша.
const LEGEND = "Красные — ниже 250 ppi, зелёные — 250 ppi и выше. PitStop считает низким разрешение ниже 275 ppi.";
const BUTTON_TITLE = "Callas подпишет на каждом цветном и полутоновом изображении PDF заказа его разрешение (ppi). Картинку можно скачать и приложить к возврату клиенту. PDF заказа не меняется.";
const REPLACE_CONFIRM = "У заказа уже выбрано пользовательское превью для возврата. Заменить его разметкой разрешения?";

const props = defineProps({
  runId: { type: String, required: true },
  orderId: { type: [String, Number], required: true },
  order: { type: Object, required: true },
});
// attached(url) — картинка стала превью для возврата: карточка показывает ссылку «Пользовательское превью…».
const emit = defineEmits(["attached"]);

const result = ref(null);
const busy = ref(false);
const attaching = ref(false);
const attached = ref(false);
const error = ref("");
const zoomed = ref(false);

const lowGroup = computed(() => lowResolutionGroup(props.order));
const recommended = computed(() => Boolean(lowGroup.value));
const title = computed(() => recommended.value
  ? `PitStop нашёл изображения с низким разрешением. ${BUTTON_TITLE}`
  : BUTTON_TITLE);
// Список изображений есть, только если бэкенд знает ppi каждого (сейчас отчёт профиля их не даёт).
const lowByPage = computed(() => {
  const images = Array.isArray(result.value?.images) ? result.value.images : [];
  const pages = new Map();
  for (const image of images) {
    if (!image?.low) continue;
    const list = pages.get(image.page) || [];
    list.push(`${Math.round(Number(image.ppi))} ppi`);
    pages.set(image.page, list);
  }
  return [...pages.entries()].sort((a, b) => a[0] - b[0]).map(([page, values]) => `Стр. ${page}: ${values.join(", ")}`);
});
const markedText = computed(() => {
  const count = Number(result.value?.marked_images);
  return Number.isFinite(count) && count > 0 ? `Размечено изображений: ${count}` : "";
});

async function show() {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    result.value = await api.lowResPreview(props.runId, props.orderId);
    attached.value = false;
  } catch (e) {
    error.value = e?.message || "Не удалось построить разметку разрешения.";
  } finally {
    busy.value = false;
  }
}

async function attach() {
  if (attaching.value || !result.value) return;
  if (props.order.custom_preview_url && !window.confirm(REPLACE_CONFIRM)) return;
  attaching.value = true;
  error.value = "";
  try {
    const payload = await api.attachLowResPreview(props.runId, props.orderId);
    attached.value = true;
    emit("attached", payload.custom_preview_url || payload.url);
  } catch (e) {
    error.value = e?.message || "Не удалось приложить разметку к возврату.";
  } finally {
    attaching.value = false;
  }
}
</script>

<template>
  <div class="lowres-preview">
    <div class="callas-fix-controls">
      <button
        type="button"
        class="callas-size-fix callas-extra-fix lowres-preview-button"
        :class="{ recommended }"
        :title="title"
        aria-label="Показать разрешение изображений"
        :aria-busy="busy"
        :disabled="busy"
        @click="show"
      >
        <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
          <rect x="3" y="4" width="14" height="12" rx="1.5" /><path d="m3 13 4-4 3 3 2-2 5 5" /><circle cx="13" cy="7.5" r="1.3" />
        </svg>
        <span>{{ busy ? "Разметка…" : "Показать разрешение" }}</span>
        <i v-if="recommended" class="fix-recommended-dot" aria-label="рекомендовано проверкой" title="PitStop нашёл низкое разрешение"></i>
      </button>
    </div>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <div v-if="result" class="lowres-result">
      <p class="lowres-legend">{{ result.legend || LEGEND }}</p>
      <ul v-if="lowByPage.length" class="lowres-list"><li v-for="line in lowByPage" :key="line">{{ line }}</li></ul>
      <p v-else-if="markedText" class="lowres-count muted">{{ markedText }}</p>
      <div class="preview-image lowres-image" role="button" tabindex="0" aria-label="Открыть разметку разрешения крупно" @click="zoomed = true" @keydown.enter="zoomed = true">
        <PreviewImage :key="result.image_url" :src="result.image_url" alt="Разметка разрешения изображений" :eager="true" />
      </div>
      <div class="lowres-actions">
        <a class="button secondary small" :href="result.image_url" download>Скачать JPEG</a>
        <a class="button secondary small" :href="result.pdf_url" download>Скачать PDF с разметкой</a>
        <button type="button" class="button secondary small lowres-attach" :disabled="attaching" @click="attach">{{ attaching ? "Прикладываем…" : "Приложить к возврату" }}</button>
      </div>
      <small v-if="attached" class="lowres-attached" role="status">Разметка выбрана превью для возврата клиенту</small>
    </div>
    <Teleport to="body">
      <div v-if="zoomed && result" class="lightbox" @click.self="zoomed = false">
        <button class="icon-button" @click="zoomed = false">✕</button>
        <PreviewImage :src="result.image_url" alt="Разметка разрешения изображений" :eager="true" />
      </div>
    </Teleport>
  </div>
</template>
