<script setup>
defineProps({ order: Object, busy: Boolean });
defineEmits(["decide"]);
</script>
<template>
  <section v-if="order.status === 'waiting_confirmation' || order.correction" class="decision-box">
    <div><strong>Требуется решение по коррекции</strong><p>{{ order.correction?.message || "Проверьте предложенные изменения размера." }}</p></div>
    <div v-if="order.correction?.original || order.correction?.proposed" class="decision-values">
      <span>Было: <b>{{ order.correction.original }}</b></span><span>Станет: <b>{{ order.correction.proposed }}</b></span>
    </div>
    <div class="decision-actions"><button class="button success" :disabled="busy" @click="$emit('decide', 'confirm')">Применить</button><button class="button secondary" :disabled="busy" @click="$emit('decide', 'reject')">Оставить без изменений</button></div>
  </section>
</template>
