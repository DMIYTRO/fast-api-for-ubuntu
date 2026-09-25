<script setup>
import { computed, ref } from "vue";
import PreviewImage from "./PreviewImage.vue";

const props = defineProps({ files: Array, hasProductionPdf: { type: Boolean, default: false } });
const emit = defineEmits(["retry-layered-tiff"]);
const zoomed = ref(null);
const rotation = ref({});

const bySide = computed(() => {
  const files = props.files || [];
  const sideOf = (file) => String(file.side || file.parsed?.side || "").toLowerCase();
  const duplexPdf = files.find((file) =>
    Number(file.page_count) === 2 && Array.isArray(file.preview_paths) && file.preview_paths.length >= 2
  );
  if (duplexPdf) {
    const page = (number, side) => {
      const thumbnail = withQuery(previewUrl(duplexPdf), "page", number);
      return {
        ...duplexPdf,
        id: `${duplexPdf.id}:${side}`,
        side,
        filename: `${duplexPdf.filename || duplexPdf.name} - ${side}`,
        thumbnail_url: thumbnail,
        full_preview_url: withQuery(withQuery(duplexPdf.full_preview_url || previewUrl(duplexPdf), "size", "full"), "page", number),
      };
    };
    return [
      { side: "face", file: page(1, "face") },
      { side: "back", file: page(2, "back") },
    ];
  }
  const face = files.find((file) => sideOf(file) === "face") || files[0] || null;
  const back = files.find((file) => sideOf(file) === "back") || null;
  return [
    { side: "face", file: face },
    ...(back ? [{ side: "back", file: back }] : []),
  ];
});

const previewUrl = (file) => file?.thumbnail_url || file?.preview_url || (file?.id ? `/api/files/${encodeURIComponent(file.id)}/preview?size=thumbnail` : "");
const withQuery = (url, key, value) => {
  const [path, query = ""] = url.split("?");
  const params = new URLSearchParams(query);
  params.set(key, value);
  return `${path}?${params.toString()}`;
};
const fullPreviewUrl = (file) => file?.full_preview_url || withQuery(previewUrl(file), "size", "full");

function rotate(file) { rotation.value[file.id] = ((rotation.value[file.id] || 0) + 90) % 360; }
function imageTransform(file) {
  const angle = rotation.value[file.id] || 0;
  const scale = angle % 180 ? 0.55 : 1;
  return `rotate(${angle}deg) scale(${scale})`;
}


</script>

<template>
  <div class="preview-pair" :class="{ single: bySide.length === 1 }">
    <div v-for="{ side, file } in bySide" :key="`${side}:${file?.id || 'none'}`" class="preview-cell">
      <div class="preview-label">
        <b>{{ side === "face" ? "Face" : "Back" }}</b>
        <button v-if="file" title="Повернуть" @click="rotate(file)">↻</button>
      </div>
      <div v-if="file && previewUrl(file)" class="preview-image" role="button" tabindex="0" :aria-label="`Открыть подробное превью: ${file.filename}`" @click="zoomed = file" @keydown.enter="zoomed = file">
        <PreviewImage
          :key="`${file.id}:${previewUrl(file)}:${file.preview_paths?.join(',') || ''}`"
          :src="previewUrl(file)"
          :alt="`${side}: ${file.filename}`"
          :style="{ transform: imageTransform(file) }"
          :process-on-retry="Boolean(file.layered_tiff_export_url)"
          :processing="Boolean(file.layered_tiff_export_processing)"
          :retry-label="file.layered_tiff_export_error ? 'Повторить создание PDF' : 'Повторить загрузку'"
          @retry="emit('retry-layered-tiff', file)"
        />
      </div>
      <small v-if="file?.layered_tiff_export_error" class="preview-export-error" role="alert">{{ file.layered_tiff_export_error }}</small>
      <a v-if="file?.layered_tiff_export_pdf_url" class="preview-export-pdf-link" :href="file.layered_tiff_export_pdf_url" target="_blank">Открыть сгенерированный PDF ↗</a>
      <div v-if="!file && !props.hasProductionPdf" class="preview-empty">Нет файла</div>
      <div v-else-if="file && !previewUrl(file) && !props.hasProductionPdf" class="preview-empty">Превью создается...</div>
      <div v-if="file" class="preview-filename" :title="file.filename">{{ file.filename }}</div>
    </div>
  </div>
  <Teleport to="body">
    <div v-if="zoomed" class="lightbox" @click.self="zoomed = null">
      <button class="icon-button" @click="zoomed = null">✕</button>
      <PreviewImage :src="fullPreviewUrl(zoomed)" :alt="zoomed.filename" :eager="true" />
    </div>
  </Teleport>
</template>
