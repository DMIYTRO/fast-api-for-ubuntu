<script setup>
import { computed, ref } from "vue";
const props = defineProps({ kits: { type: Array, default: () => [] }, count: Number, selectedIds: Array, comments: Object, canPrint: Boolean, needsPrintConfirmation: Boolean, blockReason: String, busy: Boolean, reasons: Array });
const emit = defineEmits(["clear", "action"]);
const mode = ref("");
const comment = ref("");
const selectedReasons = ref([]);
const reasonSearch = ref("");
const filteredReasons = computed(() => (props.reasons || []).filter((item) =>
  `${item.category} ${item.text}`.toLowerCase().includes(reasonSearch.value.toLowerCase())
));
const preparedCount = computed(() => (props.selectedIds || []).filter((id) => props.comments?.[id]).length);
const allHavePreparedComments = computed(() => preparedCount.value === props.count);
function prepare(action) {
  if (action === "print" && props.needsPrintConfirmation) mode.value = "force-print";
  else if (action === "print") emit("action", action, "");
  else mode.value = "reject";
}
function submit() {
  const reasonText = selectedReasons.value.join("; ");
  const value = [reasonText, comment.value].filter(Boolean).join(". ");
  emit("action", "reject", value);
  mode.value = ""; comment.value = ""; selectedReasons.value = [];
}
function confirmForcedPrint() {
  mode.value = "";
  emit("action", "print", "", true);
}
</script>
<template>
  <Teleport to="body"><section v-if="count" class="action-bar" aria-live="polite">
    <strong>Выбрано: {{ count }}</strong>
    <button class="button ghost" @click="$emit('clear')">Снять выделение</button>
    <div class="action-spacer"></div>
    <button class="button danger" @click="prepare('reject')">Вернуть на доработку</button>
    <button class="button success" :disabled="!canPrint || busy" :title="!canPrint ? (blockReason || 'В печать можно отправить только прошедшие заказы') : ''" @click="prepare('print')">Провести в печать</button>
  </section>
  <div v-if="mode === 'force-print'" class="confirm-backdrop"><section class="confirm-card" role="dialog" aria-modal="true"><h3>Заказ с ошибками</h3><p>Заказ с ошибками. Отправить в печать под вашу ответственность?</p><p class="muted">Будет отправлен готовый итоговый PDF текущей версии. Решение будет записано в журнал заказа.</p><div><button class="button secondary" :disabled="busy" @click="mode = ''">Отмена</button><button class="button danger" :disabled="busy" @click="confirmForcedPrint">Отправить под мою ответственность</button></div></section></div>
  <div v-if="mode === 'reject'" class="confirm-backdrop"><form class="confirm-card" @submit.prevent="submit"><h3>Вернуть на доработку</h3><p>Для каждого заказа будет создан отдельный запрос в Sborka с его номером, комментарием и именем сформированного превью.</p>
    <div v-for="kit in kits" :key="kit.main_id" class="kit-return-warning" role="alert"><strong>Комплект {{ kit.main_id }} ({{ kit.size }} заказов, выбрано {{ kit.selected }})</strong><span>Sborka вернёт клиенту весь комплект одним возвратом через главный заказ {{ kit.main_id }}: один комментарий и одно превью. Все заказы комплекта в этой проверке тоже будут возвращены; заказы, которые ещё проверяются, — после окончания проверки.</span></div>
    <p v-if="preparedCount" class="prepared-comments">Индивидуальные причины заполнены для {{ preparedCount }} из {{ count }} заказов.</p>
    <input v-if="reasons?.length" v-model.trim="reasonSearch" type="search" placeholder="Поиск причины">
    <div v-if="reasons?.length" class="reason-picker">
      <label v-for="item in filteredReasons.slice(0, 80)" :key="`${item.category}:${item.text}`" class="check">
        <input v-model="selectedReasons" type="checkbox" :value="item.text"><span><small>{{ item.category }}</small>{{ item.text }}</span>
      </label>
    </div>
    <textarea v-model.trim="comment" rows="4" placeholder="Общий дополнительный комментарий (необязательно)"></textarea><div><button class="button secondary" type="button" @click="mode = ''">Отмена</button><button class="button danger" :disabled="busy">Подтвердить возврат</button></div></form></div></Teleport>
</template>
