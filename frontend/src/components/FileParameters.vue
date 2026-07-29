<script setup>
defineProps({ file: Object });
const value = (input, unit = "") => input != null && input !== "" ? `${input}${unit}` : "—";
const number = (input) => {
  const parsed = Number(input);
  return Number.isFinite(parsed) ? parsed.toFixed(1) : "—";
};
const dpi = (file) => {
  if (file?.pdf_content_type === "vector") return "Вектор";
  return `${number(file?.dpi_x ?? file?.pdf_min_dpi)} × ${number(file?.dpi_y ?? file?.pdf_min_dpi)}`;
};
</script>
<template>
  <div class="file-params">
    <strong :title="file.filename">{{ file.filename }}</strong>
    <dl>
      <div><dt>Размер</dt><dd>{{ number(file.width_mm ?? file.actual_width_mm) }} × {{ number(file.height_mm ?? file.actual_height_mm) }} мм</dd></div>
      <div><dt>DPI</dt><dd>{{ dpi(file) }}</dd></div>
      <div><dt>Цвет</dt><dd>{{ value(file.colorspace || file.color_space) }}</dd></div>
      <div><dt>Формат</dt><dd>{{ value(file.format) }}</dd></div>
    </dl>
    <a v-if="file.source_url || file.id" :href="file.source_url || `/api/files/${file.id}/source`" target="_blank">Открыть исходник ↗</a>
  </div>
</template>
