<script setup>
defineProps({ file: Object });
const value = (input, unit = "") => input != null && input !== "" ? `${input}${unit}` : "—";
</script>
<template>
  <div class="file-params">
    <strong :title="file.filename">{{ file.filename }}</strong>
    <dl>
      <div><dt>Размер</dt><dd>{{ value(file.width_mm, "×") }}{{ value(file.height_mm, " мм") }}</dd></div>
      <div><dt>DPI</dt><dd>{{ value(file.dpi_x) }} × {{ value(file.dpi_y) }}</dd></div>
      <div><dt>Цвет</dt><dd>{{ value(file.colorspace || file.color_space) }}</dd></div>
      <div><dt>Формат</dt><dd>{{ value(file.format) }}</dd></div>
    </dl>
    <a v-if="file.source_url || file.id" :href="file.source_url || `/api/files/${file.id}/source`" target="_blank">Открыть исходник ↗</a>
  </div>
</template>
