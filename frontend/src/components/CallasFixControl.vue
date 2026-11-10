<script setup>
import { computed, onBeforeUnmount, ref, useId, watch } from "vue";
import { api } from "../services/api.js";
import FixComparison from "./FixComparison.vue";

const POLL_MS = 1500;
const UNAVAILABLE = [404, 405, 501];

const props = defineProps({
  orderId: { type: [String, Number], required: true },
  // order.callas_fix from the order payload: { comparison: {...} }
  initial: { type: Object, default: null },
  // Show the "Исправить" button (an order has a production PDF); the block with a result is shown regardless.
  canStart: { type: Boolean, default: true },
});

const uid = useId();
const comparison = ref(null);
const plans = ref([]);
const planKey = ref("");
const panelOpen = ref(false);
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
      const payload = await api.callasFixStatus(props.orderId);
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

async function togglePanel() {
  panelOpen.value = !panelOpen.value;
  if (!panelOpen.value || plans.value.length || loadingPlans.value) return;
  loadingPlans.value = true;
  error.value = "";
  try {
    const payload = await api.callasPlans();
    const list = Array.isArray(payload) ? payload : payload?.plans || [];
    plans.value = list.filter((plan) => plan?.key);
    planKey.value = plans.value[0]?.key || "";
  } catch (e) {
    if (UNAVAILABLE.includes(e?.status)) {
      unavailable.value = true;
      panelOpen.value = false;
    } else {
      error.value = errorMessage(e, "Не удалось загрузить планы Callas.");
    }
  } finally {
    loadingPlans.value = false;
  }
}

async function start(key = planKey.value) {
  if (!key || busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const payload = await api.startCallasFix(props.orderId, key);
    if (disposed) return;
    panelOpen.value = false;
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
    const payload = await api.decideCallasFix(props.orderId, decision);
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
      <button type="button" class="button secondary small" :aria-expanded="panelOpen" :aria-controls="`callas-fix-${uid}`" aria-label="Исправить макет через Callas: выбрать план" @click="togglePanel">Исправить {{ panelOpen ? "▴" : "▾" }}</button>
      <div v-if="panelOpen" :id="`callas-fix-${uid}`" class="callas-fix-panel">
        <p v-if="loadingPlans" class="muted" role="status"><span class="spinner" aria-hidden="true"></span> Загружаем планы…</p>
        <p v-else-if="!plans.length && !error" class="muted" role="status">Нет доступных планов Callas</p>
        <template v-else-if="plans.length">
          <label :for="`callas-plan-${uid}`">План Callas</label>
          <select :id="`callas-plan-${uid}`" v-model="planKey" aria-label="План Callas" :disabled="busy">
            <option v-for="plan in plans" :key="plan.key" :value="plan.key">{{ plan.title || plan.key }}</option>
          </select>
          <button type="button" class="button primary small" :disabled="busy || !planKey" @click="start()">{{ busy ? "Запуск…" : "Запустить" }}</button>
        </template>
      </div>
    </div>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
  </div>
</template>
