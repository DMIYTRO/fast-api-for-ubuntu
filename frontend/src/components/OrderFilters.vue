<script setup>
import { computed } from "vue";
const props = defineProps({ modelValue: String, search: String, orders: Array, visibleOrders: Array, selected: Array });
defineEmits(["update:modelValue", "update:search", "toggle-all"]);
const count = (orders, type) => orders.filter((o) => type === "all" || o.status === type || (type === "passed" && o.passed) || (type === "warning" && o.warnings?.length) || (type === "error" && o.errors?.length)).length;
const visibleIds = computed(() => (props.visibleOrders || []).map((order) => String(order.order_id ?? order.id)));
const selectedVisibleCount = computed(() => visibleIds.value.filter((id) => props.selected?.includes(id)).length);
const allSelected = computed(() => visibleIds.value.length > 0 && selectedVisibleCount.value === visibleIds.value.length);
const partlySelected = computed(() => selectedVisibleCount.value > 0 && !allSelected.value);
</script>
<template>
  <div class="filter-bar">
    <label class="select-all">
      <input type="checkbox" :checked="allSelected" :indeterminate="partlySelected" :disabled="!visibleIds.length" @change="$emit('toggle-all')">
      <span>Выбрать все</span>
      <small v-if="selectedVisibleCount">{{ selectedVisibleCount }} / {{ visibleIds.length }}</small>
    </label>
    <div class="tabs" role="tablist">
      <button v-for="item in [['all','Все'],['passed','Прошли'],['warning','Предупреждения'],['error','Ошибки'],['waiting_confirmation','Нужно решение']]" :key="item[0]" :class="{ active: modelValue === item[0] }" @click="$emit('update:modelValue', item[0])">{{ item[1] }} <span>{{ count(orders, item[0]) }}</span></button>
    </div>
    <label class="search"><span>⌕</span><input :value="search" placeholder="Заказ, клиент или файл" @input="$emit('update:search', $event.target.value)"></label>
  </div>
</template>
