<script setup>
import { computed, onBeforeUnmount, ref, useId, watch } from "vue";
import { api } from "../services/api.js";
import FixComparison from "./FixComparison.vue";

const POLL_MS = 1500;
const UNAVAILABLE = [404, 405, 501];

const props = defineProps({
  runId: { type: String, required: true },
  orderId: { type: [String, Number], required: true },
  // order.callas_fix from the order payload: { comparison: {...} }
  initial: { type: Object, default: null },
  // Show the "Исправить" button (an order has a production PDF); the block with a result is shown regardless.
  canStart: { type: Boolean, default: true },
  // Исходный PDF-файл заказа без собранного PDF: результат только для просмотра и скачивания.
  fileId: { type: String, default: "" },
  fileLabel: { type: String, default: "" },
});

const uid = useId();
const comparison = ref(null);
const plans = ref([]);
const planKey = ref("");
const loadingPlans = ref(false);
const unavailable = ref(false);
const busy = ref(false);
const error = ref("");
let timer;
let disposed = false;

const status = computed(() => comparison.value?.status || "");
const processing = computed(() => status.value === "processing");
const showBlock = computed(() => Boolean(comparison.value));
const canPickPlan = computed(() => props.canStart && !unavailable.value && !processing.value);

const errorMessage = (e, fallback) => e?.message || fallback;
const extract = (payload) => payload?.comparison || (payload?.status ? payload : null);

function stopPolling() {
  clearTimeout(timer);
  timer = undefined;
}
function apply(payload) {
  const next = extract(payload);
  if (!next) return;
  comparison.value = next;
  if (next.status === "processing") schedulePoll();
  else stopPolling();
}
function schedulePoll() {
  if (disposed || timer) return;
  timer = setTimeout(async () => {
    timer = undefined;
    if (disposed) return;
    try {
      const payload = await api.callasFixStatus(props.runId, props.orderId, props.fileId);
      if (disposed) return;
      error.value = "";
      apply(payload);
      // Processing with no usable payload would otherwise stop the poll silently.
      if (!extract(payload)) schedulePoll();
    } catch (e) {
      if (disposed) return;
      error.value = errorMessage(e, "Не удалось получить состояние исправления.");
      if (!UNAVAILABLE.includes(e?.status)) schedulePoll();
    }
  }, POLL_MS);
}

watch(() => props.initial?.comparison, (value) => {
  if (value) apply({ comparison: value });
}, { immediate: true, deep: true });

// Ключ плана коррекции размера — единственное действие, доступное одной кнопкой.
const SIZE_PLAN_KEY = "size_correction";
const SIZE_BUTTON_TITLE = "Скорректировать размер макета через Callas. Будет создана исправленная копия PDF и сравнение «до / после»; исходный PDF не меняется, результат нужно принять или отклонить.";

async function loadPlans() {
  if (plans.value.length || loadingPlans.value) return plans.value;
  loadingPlans.value = true;
  error.value = "";
  try {
    const payload = await api.callasPlans();
    const list = Array.isArray(payload) ? payload : payload?.plans || [];
    plans.value = list.filter((plan) => plan?.key);
    planKey.value = plans.value.find((plan) => plan.key === SIZE_PLAN_KEY)?.key || "";
  } catch (e) {
    if (UNAVAILABLE.includes(e?.status)) {
      unavailable.value = true;
    } else {
      error.value = errorMessage(e, "Не удалось загрузить планы Callas.");
    }
  } finally {
    loadingPlans.value = false;
  }
  return plans.value;
}

// Одно нажатие: загрузить планы (один раз) и запустить коррекцию размера.
async function startSizeCorrection() {
  if (busy.value || loadingPlans.value) return;
  const list = await loadPlans();
  if (unavailable.value) return;
  const key = list.find((plan) => plan.key === SIZE_PLAN_KEY)?.key;
  if (!key) {
    if (!error.value) error.value = "План корректировки размера недоступен";
    return;
  }
  await start(key);
}

async function start(key = planKey.value) {
  if (!key || busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const payload = await api.startCallasFix(props.runId, props.orderId, key, props.fileId);
    if (disposed) return;
    stopPolling();
    const next = extract(payload);
    apply(next ? payload : { comparison: { status: "processing", plan: plans.value.find((p) => p.key === key) || { key, title: key } } });
  } catch (e) {
    if (UNAVAILABLE.includes(e?.status)) unavailable.value = true;
    error.value = errorMessage(e, "Не удалось запустить исправление Callas.");
  } finally {
    busy.value = false;
  }
}

async function decide(decision) {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const payload = await api.decideCallasFix(props.runId, props.orderId, decision, props.fileId);
    if (disposed) return;
    const next = extract(payload);
    if (next) apply(payload);
    else comparison.value = { ...comparison.value, status: decision === "accept" ? "accepted" : "rejected" };
  } catch (e) {
    error.value = errorMessage(e, "Не удалось сохранить решение.");
  } finally {
    busy.value = false;
  }
}

onBeforeUnmount(() => {
  disposed = true;
  stopPolling();
});
</script>

<template>
  <div v-if="showBlock || canStart || unavailable" class="callas-fix">
    <p v-if="fileLabel" class="callas-fix-file muted">Исходный PDF: <b>{{ fileLabel }}</b></p>
    <FixComparison
      v-if="showBlock"
      :comparison="comparison"
      :busy="busy"
      @accept="decide('accept')"
      @reject="decide('reject')"
      @retry="start(comparison.plan?.key)"
    />
    <p v-if="unavailable && !showBlock" class="callas-fix-unavailable muted" role="status">Исправление Callas недоступно</p>
    <div v-else-if="canPickPlan" class="callas-fix-controls">
      <button
        type="button"
        class="icon-button callas-size-fix"
        :title="SIZE_BUTTON_TITLE"
        aria-label="Скорректировать размер макета через Callas"
        :aria-busy="busy || loadingPlans"
        :disabled="busy || loadingPlans"
        @click="startSizeCorrection"
      >
        <svg viewBox="0 0 20 20" aria-hidden="true" focusable="false">
          <rect x="3" y="3" width="14" height="14" rx="1.5" />
          <path d="M6 10h8M10 6v8M6 7.5 6 6h1.5M14 12.5V14h-1.5" />
        </svg>
        <span class="icon-button-label">{{ busy || loadingPlans ? "Запуск…" : "Размер" }}</span>
      </button>
    </div>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
  </div>
</template>
