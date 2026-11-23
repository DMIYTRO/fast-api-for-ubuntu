<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { orderIdentity } from "../stores/orderIdentity.js";
import { CONFIDENCE_LABELS, preselectedReasons, returnCommentLines, returnDraft, returnSuggestions } from "../stores/returnDrafts.js";

const props = defineProps({ order: Object, reasons: Array });
const emit = defineEmits(["change"]);
// Большой справочник показывается свёрнутым по категориям; маленький — целиком.
const COLLAPSE_ABOVE = 60;
const open = ref(false);
const search = ref("");
const copied = ref(false);
const searchInput = ref(null);
const expanded = ref(new Set());
// Выбор хранится вне компонента: перемонтирование карточки его не сбрасывает.
const draft = computed(() => returnDraft(orderIdentity(props.order)));

const suggestions = computed(() => {
  const seen = new Set();
  return returnSuggestions(props.order).filter((item) => !seen.has(item.text) && seen.add(item.text));
});
const suggestedTexts = computed(() => new Set(suggestions.value.map((item) => item.text)));
const selectedSet = computed(() => new Set(draft.value.selected));
const query = computed(() => search.value.trim().toLowerCase());
const filteredReasons = computed(() => {
  const value = query.value;
  return (props.reasons || []).filter((item) =>
    value
      ? `${item.category} ${item.text}`.toLowerCase().includes(value)
      : !suggestedTexts.value.has(item.text)
  );
});
// Группы в порядке справочника.
const groups = computed(() => {
  const byCategory = new Map();
  for (const item of filteredReasons.value) {
    const category = item.category || "Прочее";
    if (!byCategory.has(category)) byCategory.set(category, []);
    byCategory.get(category).push(item);
  }
  return [...byCategory].map(([category, items]) => ({
    category,
    items,
    selected: items.filter((item) => selectedSet.value.has(item.text)).length,
  }));
});
const collapsible = computed(() => !query.value && (props.reasons || []).length > COLLAPSE_ABOVE);
const groupOpen = (category) => !collapsible.value || expanded.value.has(category);
const allExpanded = computed(() => groups.value.every((group) => expanded.value.has(group.category)));

const lines = computed(() => returnCommentLines(draft.value.selected, draft.value.custom));
const selectedCount = computed(() => lines.value.length);
const comment = computed(() => lines.value.map((line) => line.text).join("\n"));
const unreviewed = computed(() => suggestions.value.length > 0 && !draft.value.reviewed);
const triggerTitle = computed(() => suggestions.value.length
  ? `Причины возврата · Предложено проверкой: ${suggestions.value.length}`
  : "Причины возврата");
const summaryText = computed(() => lines.value[0]?.source.replace(/[.;\s]+$/, "") || "");
const summaryTitle = computed(() => (comment.value ? `Будет отправлено клиенту:\n${comment.value}` : ""));

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

function onKeydown(event) {
  if (event.key === "Escape") close();
}
watch(open, (value) => {
  if (value) window.addEventListener("keydown", onKeydown);
  else window.removeEventListener("keydown", onKeydown);
});
onBeforeUnmount(() => window.removeEventListener("keydown", onKeydown));

function touch() {
  draft.value.touched = true;
}
async function openDialog() {
  open.value = true;
  draft.value.reviewed = true;
  await nextTick();
  searchInput.value?.focus({ preventScroll: true });
}
function close() {
  open.value = false;
}
function toggle(text) {
  draft.value.selected = selectedSet.value.has(text)
    ? draft.value.selected.filter((item) => item !== text)
    : [...draft.value.selected, text];
  touch();
}
function removeLine(line) {
  if (line.custom) draft.value.custom = "";
  else draft.value.selected = draft.value.selected.filter((item) => item !== line.source);
  touch();
}
function clearAll() {
  draft.value.selected = [];
  draft.value.custom = "";
  touch();
}
function toggleGroup(category) {
  const next = new Set(expanded.value);
  if (next.has(category)) next.delete(category);
  else next.add(category);
  expanded.value = next;
}
function toggleAllGroups() {
  expanded.value = allExpanded.value ? new Set() : new Set(groups.value.map((group) => group.category));
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
    <button v-if="selectedCount" type="button" class="return-summary" :title="summaryTitle" :aria-label="`Причины возврата: ${summaryTitle}`" @click.stop="openDialog">
      <span class="return-summary-text">{{ summaryText }}</span>
      <b v-if="selectedCount > 1" class="return-summary-more">+{{ selectedCount - 1 }}</b>
    </button>
    <Teleport to="body">
      <div v-if="open" class="confirm-backdrop return-backdrop" @click.self="close">
        <section class="return-dialog" role="dialog" aria-modal="true" :aria-label="`Причины возврата заказа ${order.order_id ?? order.id}`">
          <header class="return-dialog-head">
            <div>
              <p class="eyebrow">Заказ № {{ order.order_id ?? order.id }}</p>
              <h3>Причины возврата</h3>
              <span>Отметьте готовые формулировки или напишите свой комментарий.</span>
            </div>
            <button type="button" class="icon-button" aria-label="Закрыть" @click="close">×</button>
          </header>

          <div class="return-body">
            <div class="return-choose">
              <div v-if="suggestions.length" class="return-suggestions">
                <p class="return-suggestions-title">Предложено проверкой</p>
                <label v-for="item in suggestions" :key="item.text" class="return-option return-suggestion" :class="{ 'is-selected': selectedSet.has(item.text) }">
                  <input type="checkbox" :checked="selectedSet.has(item.text)" @change="toggle(item.text)">
                  <span>
                    <small>{{ item.category }}<b v-if="CONFIDENCE_LABELS[item.confidence]" class="return-confidence" :class="`confidence-${item.confidence}`">{{ CONFIDENCE_LABELS[item.confidence] }}</b></small>
                    {{ item.text }}
                    <em v-if="item.evidence" class="return-evidence">{{ item.evidence }}</em>
                  </span>
                </label>
              </div>

              <div class="return-search">
                <input ref="searchInput" v-model="search" type="search" placeholder="Поиск по тексту и категории…" aria-label="Поиск по причинам">
                <button v-if="collapsible && groups.length" type="button" class="return-expand-all" @click="toggleAllGroups">{{ allExpanded ? "Свернуть все" : "Развернуть все" }}</button>
                <span v-else-if="query" class="return-found">Найдено: {{ filteredReasons.length }}</span>
              </div>
              <div class="return-options">
                <section v-for="group in groups" :key="group.category" class="return-group" :class="{ open: groupOpen(group.category) }">
                  <button type="button" class="return-group-head" :aria-expanded="groupOpen(group.category)" :disabled="!collapsible" @click="toggleGroup(group.category)">
                    <span v-if="collapsible" class="return-group-caret" aria-hidden="true">▸</span>
                    <span class="return-group-name">{{ group.category }}</span>
                    <span class="return-group-count">{{ group.items.length }}</span>
                    <span v-if="group.selected" class="return-group-selected">выбрано {{ group.selected }}</span>
                  </button>
                  <div v-show="groupOpen(group.category)" class="return-group-items">
                    <label v-for="item in group.items" :key="`${item.category}:${item.text}`" class="return-option" :class="{ 'is-selected': selectedSet.has(item.text) }">
                      <input type="checkbox" :checked="selectedSet.has(item.text)" @change="toggle(item.text)">
                      <span>{{ item.text }}</span>
                    </label>
                  </div>
                </section>
                <p v-if="!filteredReasons.length" class="muted return-empty">Причины не найдены.</p>
              </div>
            </div>

            <aside class="return-selected" aria-label="Будет отправлено клиенту">
              <div class="return-selected-head">
                <p class="return-selected-title">Будет отправлено клиенту</p>
                <button v-if="selectedCount" type="button" class="return-clear" @click="clearAll">Очистить</button>
              </div>
              <ol v-if="lines.length" class="return-lines">
                <li v-for="line in lines" :key="`${line.custom ? 'custom' : 'reason'}:${line.source}`" class="return-line" :class="{ custom: line.custom }">
                  <span class="return-line-text">{{ line.text }}</span>
                  <button type="button" class="return-line-remove" :aria-label="line.custom ? 'Удалить свой комментарий' : `Убрать: ${line.source}`" :title="line.custom ? 'Удалить свой комментарий' : 'Убрать причину'" @click="removeLine(line)">×</button>
                </li>
              </ol>
              <p v-else class="return-selected-empty">Пока ничего не выбрано. Отметьте причины в списке или напишите свой комментарий ниже.</p>
              <label class="return-custom">
                <span>Свой комментарий</span>
                <textarea v-model.trim="draft.custom" rows="3" placeholder="Добавится последней строкой" @input="touch"></textarea>
              </label>
            </aside>
          </div>

          <footer>
            <span>{{ selectedCount ? `${selectedCount} выбрано` : "Ничего не выбрано" }}</span>
            <button type="button" class="button secondary" :disabled="!comment" @click="copyComment">{{ copied ? "Скопировано" : "Копировать" }}</button>
            <button type="button" class="button primary" @click="close">Готово</button>
          </footer>
        </section>
      </div>
    </Teleport>
  </div>
</template>
