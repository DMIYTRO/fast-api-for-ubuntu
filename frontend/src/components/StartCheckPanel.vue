<script setup>
import { computed, reactive, ref, watch } from "vue";
import { api } from "../services/api.js";

const props = defineProps({ open: Boolean, config: Object, busy: Boolean });
const emit = defineEmits(["close", "start"]);
const form = reactive({
  input_path: "", direction: "digital", create_pdfs: true, generate_previews: true,
  copy_failures: true, correction_policy: "auto",
});
// «Полная проверка для всех файлов» → check_policy: "full"; иначе "auto" (по папке: Input — облегчённая, CorelDRAW — полная).
const fullCheck = ref(false);
const browsing = ref(false);
const folderData = ref(null);
const folderError = ref("");
const profiles = computed(() => props.config?.profiles || [{ id: "digital", name: "Цифровая печать" }, { id: "offset", name: "Офсетная печать" }]);
function mm(value, fallback) {
  const number = Number(value ?? fallback);
  return String(Number.isFinite(number) ? number : fallback).replace(".", ",");
}
const correctionLimit = computed(() => mm(props.config?.auto_correct_max_mm, 4));
const correctionCap = computed(() => mm(props.config?.correction_confirm_max_mm, 10));
watch(() => props.config, (value) => {
  if (!form.input_path) form.input_path = value?.default_input_path || "";
}, { immediate: true });

async function browse(path = form.input_path) {
  folderError.value = "";
  try { folderData.value = await api.folders(path); browsing.value = true; }
  catch (error) { folderError.value = error.message; }
}
function choose() {
  form.input_path = folderData.value?.path || folderData.value?.current_path || form.input_path;
  browsing.value = false;
}
function checkOptions(inputPath = form.input_path) {
  return {
    ...form,
    input_path: inputPath,
    check_policy: fullCheck.value ? "full" : "auto",
    approve_corrections: form.correction_policy === "auto",
  };
}
function submit() { emit("start", checkOptions()); }
defineExpose({ checkOptions });
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="drawer-backdrop" @click.self="$emit('close')">
      <aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="new-check-title">
        <header class="drawer-head"><div><p class="eyebrow">Параметры запуска</p><h2 id="new-check-title">Новая проверка</h2></div><button class="icon-button" aria-label="Закрыть" @click="$emit('close')">×</button></header>
        <form class="drawer-body" @submit.prevent="submit">
          <label for="input-path">Папка с заказами</label>
          <div class="input-action"><input id="input-path" v-model.trim="form.input_path" required><button class="button secondary" type="button" @click="browse()">Выбрать</button></div>
          <p v-if="folderError" class="form-error">{{ folderError }}</p>
          <label for="profile">Профиль печати</label>
          <select id="profile" v-model="form.direction"><option v-for="profile in profiles" :key="profile.id" :value="profile.id">{{ profile.name }}</option></select>
          <fieldset><legend>Результаты</legend>
            <label class="check"><input v-model="form.create_pdfs" type="checkbox"> Создать PDF прошедших заказов</label>
            <label class="check"><input v-model="form.generate_previews" type="checkbox"> Создать превью с рамками</label>
            <label class="check"><input v-model="form.copy_failures" type="checkbox"> Копировать проблемные файлы</label>
          </fieldset>
          <fieldset><legend>Спорные коррекции</legend>
            <label class="radio"><input v-model="form.correction_policy" value="ask" type="radio"> Запрашивать решение по каждой коррекции</label>
            <label class="radio" title="Порядок коррекции размера: открыть скрытые поля → дорисовать поля Callas → растянуть (PDF) → сжать → подогнать. Файлы с (SRA3), (SRA3P) или (BAN) в имени не масштабируются: только белые поля или обрезка."><input v-model="form.correction_policy" value="auto" type="radio"> Автоматически принимать малые коррекции (поля, до {{ correctionLimit }} мм обрезки; до {{ correctionCap }} мм — с подтверждением)</label>
            <label class="radio"><input v-model="form.correction_policy" value="reject" type="radio"> Не применять коррекции</label>
          </fieldset>
          <fieldset><legend>Проверка файлов</legend>
            <label class="check"><input v-model="fullCheck" type="checkbox"> Полная проверка для всех файлов</label>
            <small class="policy-field">Иначе файлы из папки Input проверяются облегчённо, из CorelDRAW — полностью</small>
          </fieldset>
          <div class="safe-note">Исходные файлы не изменяются. Результаты сохраняются в отдельные папки.</div>
          <button class="button primary wide" :disabled="busy">{{ busy ? "Запускаем…" : "Начать проверку" }}</button>
        </form>
      </aside>
      <div v-if="browsing" class="folder-modal" role="dialog" aria-modal="true">
        <header><strong>Выберите папку</strong><button class="icon-button" @click="browsing = false">×</button></header>
        <p class="folder-path">{{ folderData?.path || folderData?.current_path }}</p>
        <div class="folder-list">
          <button v-if="folderData?.parent" @click="browse(folderData.parent)">↰ На уровень выше</button>
          <button v-for="folder in (folderData?.folders || folderData?.items || [])" :key="folder.path || folder.name" @click="browse(folder.path || folder.value)">📁 {{ folder.name || folder.label }}</button>
        </div>
        <button class="button primary wide" @click="choose">Выбрать эту папку</button>
      </div>
    </div>
  </Teleport>
</template>
