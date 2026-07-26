<script setup>
import { computed, ref } from "vue";
const props = defineProps({ count: Number, selectedIds: Array, comments: Object, canPrint: Boolean, busy: Boolean, reasons: Array });
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
function prepare(action) { if (action === "print") emit("action", action, ""); else mode.value = "reject"; }
function submit() {
  const reasonText = selectedReasons.value.join("; ");
  const value = [reasonText, comment.value].filter(Boolean).join(". ");
  if (!value && !allHavePreparedComments.value) return;
  emit("action", "reject", value);
  mode.value = ""; comment.value = ""; selectedReasons.value = [];
}
</script>
<template>
  <Teleport to="body"><section v-if="count" class="action-bar" aria-live="polite">
    <strong>Выбрано: {{ count }}</strong>
    <button class="button ghost" @click="$emit('clear')">Снять выделение</button>
    <div class="action-spacer"></div>
    <button class="button danger" @click="prepare('reject')">Вернуть на доработку</button>
    <button class="button success" :disabled="!canPrint || busy" :title="!canPrint ? 'В печать можно отправить только прошедшие заказы' : ''" @click="prepare('print')">Провести в печать</button>
  </section>
  <div v-if="mode === 'reject'" class="confirm-backdrop"><form class="confirm-card" @submit.prevent="submit"><h3>Подготовить возврат</h3><p>Выберите общую причину или используйте индивидуальные причины, заданные кнопкой 💬 в карточках.</p>
    <p v-if="preparedCount" class="prepared-comments">Индивидуальные причины заполнены для {{ preparedCount }} из {{ count }} заказов.</p>
    <input v-if="reasons?.length" v-model.trim="reasonSearch" type="search" placeholder="Поиск причины">
    <div v-if="reasons?.length" class="reason-picker">
      <label v-for="item in filteredReasons.slice(0, 80)" :key="`${item.category}:${item.text}`" class="check">
        <input v-model="selectedReasons" type="checkbox" :value="item.text"><span><small>{{ item.category }}</small>{{ item.text }}</span>
      </label>
    </div>
    <textarea v-model.trim="comment" rows="4" placeholder="Общий дополнительный комментарий"></textarea><div><button class="button secondary" type="button" @click="mode = ''">Отмена</button><button class="button danger" :disabled="busy || (!comment && !selectedReasons.length && !allHavePreparedComments)">Подтвердить возврат</button></div></form></div></Teleport>
</template>
