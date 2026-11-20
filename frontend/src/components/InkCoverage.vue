<script setup>
import { computed } from "vue";
import { inkCoverageFlagLabel, inkCoverageFlagTitle, inkCoverageFlags, inkCoverageMainFlag, lightAreaPercent } from "./inkCoverage.js";

const props = defineProps({ data: Object });

const pages = computed(() => Array.isArray(props.data?.pages) ? props.data.pages : []);
const thresholds = computed(() => props.data?.thresholds || null);
const mainFlag = computed(() => inkCoverageMainFlag(props.data));
const title = computed(() => inkCoverageFlagTitle(mainFlag.value));
const flagsByPage = computed(() => {
  const map = new Map();
  for (const flag of inkCoverageFlags(props.data)) map.set(flag.page, [...(map.get(flag.page) || []), flag]);
  return map;
});
const light = computed(() => props.data?.light_areas || null);
const lightChecked = computed(() => light.value?.status === "completed");
const pageFlags = (page) => flagsByPage.value.get(page.page) || [];
const pageClass = (page) => pageFlags(page).map((flag) => flag.kind).join(" ");
const pct = (value) => {
  if (value == null) return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return `${Math.abs(parsed - Math.round(parsed)) < 0.05 ? Math.round(parsed) : parsed.toFixed(1)}%`;
};
const pageMin = (page) => {
  const inks = Array.isArray(page?.inks) ? page.inks : [];
  if (!inks.length) return null;
  return Math.min(...inks.map((ink) => Number(ink.percent) || 0));
};
const pageMax = (page) => {
  const inks = Array.isArray(page?.inks) ? page.inks : [];
  if (!inks.length) return null;
  return Math.max(...inks.map((ink) => Number(ink.percent) || 0));
};
const inksLabel = (page) => (Array.isArray(page?.inks) ? page.inks : [])
  .map((ink) => `${ink.name} ${pct(ink.percent)}`)
  .join(", ") || "красок нет";
const kindLabel = (kind) => inkCoverageFlagLabel({ kind });
</script>

<template>
  <section v-if="data" class="ink-coverage" :class="{ flagged: Boolean(mainFlag) }">
    <header>
      <strong>Красочность (callas)</strong>
      <span v-if="data.status === 'completed'" class="muted">
        среднее покрытие по {{ data.box || "рамке" }}, {{ data.resolution || "—" }} ppi
        <template v-if="thresholds?.min_percent != null"> · светлые участки: сумма красок ниже {{ pct(thresholds.min_percent) }}</template>
        <template v-if="thresholds?.max_percent != null"> · перелив: выше {{ pct(thresholds.max_percent) }}</template>
      </span>
      <span v-if="mainFlag" class="ink-flag" :title="title">{{ kindLabel(mainFlag.kind) }}</span>
    </header>
    <p v-if="data.status === 'failed'" class="ink-error">Красочность не рассчитана: {{ data.error || "ошибка службы callas" }}</p>
    <p v-else-if="data.status !== 'completed' || !pages.length" class="muted">Красочность не проверялась.</p>
    <table v-else class="ink-table">
      <thead>
        <tr><th>Стр.</th><th>Сумма</th><th>Триадные</th><th v-if="lightChecked">Светлые участки</th><th>Мин. краска</th><th>Макс. краска</th><th>Краски</th></tr>
      </thead>
      <tbody>
        <tr v-for="page in pages" :key="page.page" :class="pageClass(page)">
          <th>{{ page.page }}</th>
          <td><b>{{ pct(page.total_sum_percent) }}</b><i v-for="flag in pageFlags(page)" :key="flag.kind" :title="inkCoverageFlagTitle(flag)"> {{ kindLabel(flag.kind) }}</i></td>
          <td>{{ pct(page.process_sum_percent) }}</td>
          <td v-if="lightChecked" class="ink-light">{{ lightAreaPercent(data, page.page) == null ? "—" : `${pct(lightAreaPercent(data, page.page))} площади` }}</td>
          <td>{{ pct(pageMin(page)) }}</td>
          <td>{{ pct(pageMax(page)) }}</td>
          <td class="ink-list" :title="inksLabel(page)">{{ inksLabel(page) }}</td>
        </tr>
      </tbody>
    </table>
    <small v-if="data.status === 'completed' && pages.length" class="muted">
      Документ: мин. {{ pct(data.document?.min_percent) }} (стр. {{ data.document?.min_page ?? "—" }}), макс. {{ pct(data.document?.max_percent) }} (стр. {{ data.document?.max_page ?? "—" }}). «Сумма» — среднее покрытие по рамке, не TAC.
      <template v-if="lightChecked"> «Светлые участки» — доля страницы, где сумма красок в точке от {{ pct(light.floor_percent) }} до {{ pct(light.tac_max_percent) }} на участках шире {{ light.min_width_mm }} мм; флаг — от {{ pct(light.min_area_percent) }} площади.</template>
    </small>
    <p v-if="data.status === 'completed' && light?.status === 'failed'" class="ink-error">Светлые участки не проверены: {{ light.error || "ошибка цветоделений callas" }}</p>
  </section>
</template>

<style scoped>
.ink-coverage{padding:10px;background:#f7f8fb;border-radius:9px;font-size:10px}
.ink-coverage header{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:6px}
.ink-coverage header strong{font-size:10px}
.ink-coverage .muted{color:#8a94a6}
.ink-flag{padding:2px 6px;border-radius:99px;background:#fff0f1;color:#a82e3b;font-weight:850;cursor:help}
.ink-error{margin:0;color:#a82e3b}
.ink-table{width:100%;border-collapse:collapse}
.ink-table th,.ink-table td{padding:4px 6px;text-align:left;border-bottom:1px solid #e3e7ee;white-space:nowrap}
.ink-table thead th{font-size:8px;text-transform:uppercase;color:#8a94a6}
.ink-table tbody tr:last-child>*{border-bottom:0}
.ink-table tr.overflow td b,.ink-table tr.underflow td b{color:#a82e3b}
.ink-table tr.light_areas td.ink-light{color:#a82e3b;font-weight:800}
.ink-table td i{margin-left:4px;font-style:normal;font-weight:800;color:#a82e3b}
.ink-list{max-width:260px;overflow:hidden;text-overflow:ellipsis}
.ink-coverage small{display:block;margin-top:6px}
</style>
