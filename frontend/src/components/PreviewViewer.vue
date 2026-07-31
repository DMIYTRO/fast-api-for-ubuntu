<script setup>
import { computed, ref } from "vue";
const props = defineProps({ files: Array });
const zoomed = ref(null);
const rotation = ref({});
const previewFiles = computed(() => (props.files || []).map((file, index) => ({
  file,
  label: String(file.side || "").toLowerCase() || `Файл ${index + 1}`,
})));
const previewUrl = (file) => file?.preview_url || (file?.id ? `/api/files/${encodeURIComponent(file.id)}/preview` : "");
function rotate(file) { rotation.value[file.id] = ((rotation.value[file.id] || 0) + 90) % 360; }
function imageTransform(file) {
  const angle = rotation.value[file.id] || 0;
  const scale = angle % 180 ? 0.55 : 1;
  return `rotate(${angle}deg) scale(${scale})`;
}
</script>

<template>
  <div class="preview-pair" :class="{ single: previewFiles.length === 1 }">
    <div v-for="{ label, file } in previewFiles" :key="file.id" class="preview-cell">
      <div class="preview-label"><b>{{ label }}</b><button title="Повернуть" @click="rotate(file)">↻</button></div>
      <button v-if="file && previewUrl(file)" class="preview-image" @click="zoomed = file">
        <img :src="previewUrl(file)" :alt="`${label}: ${file.filename}`" :style="{ transform: imageTransform(file) }">
      </button>
      <div v-else class="preview-empty">Превью отсутствует</div>
      <div class="preview-filename" :title="file.filename">{{ file.filename }}</div>
    </div>
  </div>
  <Teleport to="body"><div v-if="zoomed" class="lightbox" @click.self="zoomed = null"><button class="icon-button" @click="zoomed = null">×</button><img :src="previewUrl(zoomed)" :alt="zoomed.filename"></div></Teleport>
</template>
