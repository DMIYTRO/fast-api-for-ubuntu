<script setup>
import { computed } from "vue";

const props = defineProps({ order: Object, busy: Boolean });
defineEmits(["decide"]);

const fileName = (file) => file.name || file.filename || file.path || "файл";
const files = computed(() => props.order?.files || props.order?.file_results || []);
// Файлы, для которых принято исправление Callas исходного PDF: решение ниже их не затрагивает.
const fixedFiles = computed(() => files.value.filter((file) => file.callas_source_fix?.status === "accepted").map(fileName));
const pendingFiles = computed(() => files.value.filter((file) => file.resample_decision === "ask_confirmation").map(fileName));
</script>
<template>
  <section v-if="order.status === 'waiting_confirmation' || order.correction" class="decision-box">
    <div>
      <strong>Требуется решение по коррекции</strong><p>{{ order.correction?.message || "Проверьте предложенные изменения размера." }}</p>
      <p v-if="fixedFiles.length" class="decision-scope" role="note">
        Будут собраны из PDF, исправленного Callas: <b>{{ fixedFiles.join(", ") }}</b>.<template v-if="pendingFiles.length"> «Применить» и «Оставить без изменений» относятся только к файлам: <b>{{ pendingFiles.join(", ") }}</b>.</template>
      </p>
    </div>
    <div v-if="order.correction?.original || order.correction?.proposed" class="decision-values">
      <span>Было: <b>{{ order.correction.original }}</b></span><span>Станет: <b>{{ order.correction.proposed }}</b></span>
    </div>
    <div class="decision-actions"><button class="button success" :disabled="busy" @click="$emit('decide', 'confirm')">Применить</button><button class="button secondary" :disabled="busy" @click="$emit('decide', 'reject')">Оставить без изменений</button></div>
  </section>
</template>
