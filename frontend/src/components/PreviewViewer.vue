<script setup>
import { computed, ref } from "vue";

const props = defineProps({ files: Array });
const zoomed = ref(null);
const rotation = ref({});

const bySide = computed(() => {
  const files = props.files || [];
  const sideOf = (file) => String(file.side || file.parsed?.side || "").toLowerCase();
  const duplexPdf = files.find((file) =>
    Number(file.page_count) === 2 && Array.isArray(file.preview_paths) && file.preview_paths.length >= 2
  );
  if (duplexPdf) {
    const page = (number, side) => ({
      ...duplexPdf,
      id: `${duplexPdf.id}:${side}`,
      side,
      filename: `${duplexPdf.filename || duplexPdf.name} - ${side}`,
      preview_url: `${previewUrl(duplexPdf)}?page=${number}`,
    });
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

const previewUrl = (file) => file?.preview_url || (file?.id ? `/api/files/${encodeURIComponent(file.id)}/preview` : "");

function rotate(file) { rotation.value[file.id] = ((rotation.value[file.id] || 0) + 90) % 360; }
function imageTransform(file) {
  const angle = rotation.value[file.id] || 0;
  const scale = angle % 180 ? 0.55 : 1;
  return `rotate(${angle}deg) scale(${scale})`;
}

function handleImgError(event, file) {
  const target = event.target;
  if (!target || target.dataset.retried === "done") return;
  const retryCount = Number(target.dataset.retries || 0);
  if (retryCount >= 3) {
    target.dataset.retried = "done";
    return;
  }
  target.dataset.retries = String(retryCount + 1);
  setTimeout(() => {
    const url = previewUrl(file);
    if (url) {
      target.src = `${url}${url.includes('?') ? '&' : '?'}retry=${Date.now()}`;
    }
  }, 1200);
}
</script>

<template>
  <div class="preview-pair" :class="{ single: bySide.length === 1 }">
    <div v-for="{ side, file } in bySide" :key="`${side}:${file?.id || 'none'}`" class="preview-cell">
      <div class="preview-label">
        <b>{{ side === "face" ? "Face" : "Back" }}</b>
        <button v-if="file" title="Повернуть" @click="rotate(file)">↻</button>
      </div>
      <button v-if="file && previewUrl(file)" class="preview-image" @click="zoomed = file">
        <img 
          :key="`${file.id}:${previewUrl(file)}:${file.preview_paths?.join(',') || ''}`"
          :src="previewUrl(file)" 
          :alt="`${side}: ${file.filename}`" 
          :style="{ transform: imageTransform(file) }"
          loading="lazy"
          decoding="async"
          @error="handleImgError($event, file)"
        >
      </button>
      <div v-else class="preview-empty">{{ file ? "Превью создается..." : "Нет файла" }}</div>
      <div v-if="file" class="preview-filename" :title="file.filename">{{ file.filename }}</div>
    </div>
  </div>
  <Teleport to="body">
    <div v-if="zoomed" class="lightbox" @click.self="zoomed = null">
      <button class="icon-button" @click="zoomed = null">✕</button>
      <img :src="previewUrl(zoomed)" :alt="zoomed.filename" decoding="async">
    </div>
  </Teleport>
</template>
