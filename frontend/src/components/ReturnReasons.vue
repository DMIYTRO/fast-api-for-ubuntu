<script setup>
import { computed, ref, watch } from "vue";
import { orderIdentity } from "../stores/orderIdentity.js";
import { CONFIDENCE_LABELS, buildReturnComment, preselectedReasons, returnDraft, returnSuggestions } from "../stores/returnDrafts.js";

const props = defineProps({ order: Object, reasons: Array });
const emit = defineEmits(["change"]);
const open = ref(false);
const search = ref("");
const copied = ref(false);
// Выбор хранится вне компонента: перемонтирование карточки его не сбрасывает.
const draft = computed(() => returnDraft(orderIdentity(props.order)));

const suggestions = computed(() => {
  const seen = new Set();
  return returnSuggestions(props.order).filter((item) => !seen.has(item.text) && seen.add(item.text));
});
const suggestedTexts = computed(() => new Set(suggestions.value.map((item) => item.text)));
const filteredReasons = computed(() => {
  const query = search.value.trim().toLowerCase();
  return (props.reasons || []).filter((item) =>
    query
      ? `${item.category} ${item.text}`.toLowerCase().includes(query)
      : !suggestedTexts.value.has(item.text)
  );
});
const selectedCount = computed(() => draft.value.selected.length + (draft.value.custom.trim() ? 1 : 0));
const comment = computed(() => buildReturnComment(draft.value.selected, draft.value.custom));
const unreviewed = computed(() => suggestions.value.length > 0 && !draft.value.reviewed);
const triggerTitle = computed(() => suggestions.value.length
  ? `Причины возврата · Предложено проверкой: ${suggestions.value.length}`
  : "Причины возврата");

// Предвыбор: пока оператор не трогал выбор, он повторяет уверенные предложения
// проверки и следует за их изменениями (перепроверка, SSE).
const preselectKey = computed(() => preselectedReasons(props.order).join("\n"));
watch([preselectKey, draft], ([key, current]) => {
  if (current.touched || current.seededKey === key) return;
  current.selected = key ? key.split("\n") : [];
  current.seededKey = key;
}, { immediate: true });

watch(comment, (value) => emit("change", value), { immediate: true });
watch(
  () => `${props.order.action_result?.action || ""}:${props.order.action_result?.status || ""}`,
  (value) => {
    if (value === "reject:prepared") {
      draft.value.selected = [];
      draft.value.custom = "";
      draft.value.touched = true;
      search.value = "";
    }
  }
);

function touch() {
  draft.value.touched = true;
}
function openDialog() {
  open.value = true;
  draft.value.reviewed = true;
}
function remove(reason) {
  draft.value.selected = draft.value.selected.filter((item) => item !== reason);
  touch();
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
    <button type="button" class="return-trigger" :class="{ 'has-suggestions': unreviewed }" :title="triggerTitle" :aria-label="triggerTitle" @click.stop="openDialog">
      <span>💬</span>
      <span v-if="selectedCount" class="return-badge">{{ selectedCount }}</span>
      <span v-else-if="unreviewed" class="return-suggestion-dot" aria-hidden="true"></span>
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

          <div v-if="suggestions.length" class="return-suggestions">
            <p class="return-suggestions-title">Предложено проверкой</p>
            <label v-for="item in suggestions" :key="item.text" class="return-option return-suggestion">
              <input v-model="draft.selected" type="checkbox" :value="item.text" @change="touch">
              <span>
                <small>{{ item.category }}<b v-if="CONFIDENCE_LABELS[item.confidence]" class="return-confidence" :class="`confidence-${item.confidence}`">{{ CONFIDENCE_LABELS[item.confidence] }}</b></small>
                {{ item.text }}
                <em v-if="item.evidence" class="return-evidence">{{ item.evidence }}</em>
              </span>
            </label>
          </div>

          <input v-model.trim="search" type="search" placeholder="Поиск по причинам…">
          <div class="return-options">
            <label v-for="item in filteredReasons" :key="`${item.category}:${item.text}`" class="return-option">
              <input v-model="draft.selected" type="checkbox" :value="item.text" @change="touch">
              <span><small>{{ item.category }}</small>{{ item.text }}</span>
            </label>
            <p v-if="!filteredReasons.length" class="muted">Причины не найдены.</p>
          </div>

          <div v-if="draft.selected.length" class="reason-chips">
            <span v-for="reason in draft.selected" :key="reason">{{ reason }}<button type="button" @click="remove(reason)">×</button></span>
          </div>
          <textarea v-model.trim="draft.custom" rows="3" placeholder="Дополнительный комментарий" @input="touch"></textarea>

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
