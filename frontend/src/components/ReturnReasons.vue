<script setup>
import { computed, ref, watch } from "vue";

const props = defineProps({ order: Object, reasons: Array });
const emit = defineEmits(["change"]);
const open = ref(false);
const search = ref("");
const selected = ref([]);
const custom = ref("");
const copied = ref(false);

const filteredReasons = computed(() => {
  const query = search.value.trim().toLowerCase();
  return (props.reasons || []).filter((item) =>
    !query || `${item.category} ${item.text}`.toLowerCase().includes(query)
  );
});
const selectedCount = computed(() => selected.value.length + (custom.value.trim() ? 1 : 0));
const comment = computed(() => {
  const parts = [...selected.value, custom.value.trim()].filter(Boolean);
  if (!parts.length) return "";
  if (parts.length === 1) return `${parts[0].replace(/[.;\s]+$/, "")}.`;
  return parts.map((item, index) => `${index + 1}) ${item.replace(/[.;\s]+$/, "")}${index === parts.length - 1 ? "." : ";"}`).join("\n");
});

watch(comment, (value) => emit("change", value), { immediate: true });
watch(
  () => `${props.order.action_result?.action || ""}:${props.order.action_result?.status || ""}`,
  (value) => {
    if (value === "reject:prepared") {
      selected.value = [];
      custom.value = "";
      search.value = "";
    }
  }
);

function remove(reason) {
  selected.value = selected.value.filter((item) => item !== reason);
}
async function copyComment() {
  if (!comment.value) return;
  try {
    await navigator.clipboard.writeText(`Заказ #${props.order.order_id ?? props.order.id}:\n${comment.value}`);
    copied.value = true;
    window.setTimeout(() => copied.value = false, 1200);
  } catch {
    copied.value = false;
  }
}
</script>

<template>
  <div class="return-editor">
    <button type="button" class="return-trigger" title="Причины возврата" @click.stop="open = true">
      <span>💬</span>
      <span v-if="selectedCount" class="return-badge">{{ selectedCount }}</span>
    </button>
    <Teleport to="body">
      <div v-if="open" class="confirm-backdrop return-backdrop" @click.self="open = false">
        <section class="return-dialog" role="dialog" aria-modal="true" :aria-label="`Причины возврата заказа ${order.order_id ?? order.id}`">
          <header>
            <div>
              <p class="eyebrow">Заказ № {{ order.order_id ?? order.id }}</p>
              <h3>Причины возврата</h3>
              <span>Выберите готовые формулировки или добавьте свою.</span>
            </div>
            <button type="button" class="icon-button" aria-label="Закрыть" @click="open = false">×</button>
          </header>

          <input v-model.trim="search" type="search" placeholder="Поиск по причинам…">
          <div class="return-options">
            <label v-for="item in filteredReasons" :key="`${item.category}:${item.text}`" class="return-option">
              <input v-model="selected" type="checkbox" :value="item.text">
              <span><small>{{ item.category }}</small>{{ item.text }}</span>
            </label>
            <p v-if="!filteredReasons.length" class="muted">Причины не найдены.</p>
          </div>

          <div v-if="selected.length" class="reason-chips">
            <span v-for="reason in selected" :key="reason">{{ reason }}<button type="button" @click="remove(reason)">×</button></span>
          </div>
          <textarea v-model.trim="custom" rows="3" placeholder="Дополнительный комментарий"></textarea>

          <footer>
            <span>{{ selectedCount ? `${selectedCount} выбрано` : "Ничего не выбрано" }}</span>
            <button type="button" class="button secondary" :disabled="!comment" @click="copyComment">{{ copied ? "Скопировано" : "Копировать" }}</button>
            <button type="button" class="button primary" @click="open = false">Готово</button>
          </footer>
        </section>
      </div>
    </Teleport>
  </div>
</template>
