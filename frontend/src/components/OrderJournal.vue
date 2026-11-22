<script setup>
import { computed } from "vue";
import { OUTCOME_LABELS, SEVERITY_LABELS, entryMeta, entryNotes, groupJournal, isMuted } from "./orderJournal.js";

const props = defineProps({ journal: { type: Object, default: null } });
const sections = computed(() => groupJournal(props.journal));
const formatDate = (value) => {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : new Intl.DateTimeFormat("ru-RU", { dateStyle: "medium", timeStyle: "short" }).format(date);
};
</script>

<template>
  <div class="order-journal">
    <p v-if="!sections.length" class="journal-empty">Записей в журнале нет.</p>
    <section v-for="section in sections" :key="section.kind" class="journal-section" :data-kind="section.kind">
      <h3>{{ section.title }}</h3>
      <ul>
        <li v-for="(entry, index) in section.entries" :key="`${entry.code}-${index}`" class="journal-entry" :class="{ muted: isMuted(entry) }">
          <span v-if="entry.severity" class="journal-chip" :class="entry.severity">{{ SEVERITY_LABELS[entry.severity] || entry.severity }}</span>
          <div class="journal-body">
            <strong>{{ entry.title }}</strong>
            <span v-if="entry.outcome" class="journal-outcome" :class="entry.outcome">{{ OUTCOME_LABELS[entry.outcome] || entry.outcome }}</span>
            <p v-if="entry.detail">{{ entry.detail }}</p>
            <p v-for="note in entryNotes(entry)" :key="note" class="journal-note">{{ note }}</p>
            <small v-if="entryMeta(entry, formatDate)">{{ entryMeta(entry, formatDate) }}</small>
          </div>
        </li>
      </ul>
      <details v-if="section.hidden.length" class="journal-hidden">
        <summary>Добавлено исправлением Callas ({{ section.hidden.length }})</summary>
        <ul>
          <li v-for="(entry, index) in section.hidden" :key="`${entry.code}-h${index}`" class="journal-entry muted">
            <span v-if="entry.severity" class="journal-chip" :class="entry.severity">{{ SEVERITY_LABELS[entry.severity] || entry.severity }}</span>
            <div class="journal-body">
              <strong>{{ entry.title }}</strong>
              <p v-for="note in entryNotes(entry)" :key="note" class="journal-note">{{ note }}</p>
              <small v-if="entryMeta(entry, formatDate)">{{ entryMeta(entry, formatDate) }}</small>
            </div>
          </li>
        </ul>
      </details>
    </section>
  </div>
</template>

<style scoped>
.order-journal{display:grid;gap:12px;font-size:12px}.journal-empty{margin:0;color:var(--muted)}.journal-section h3{margin:0 0 6px;font-size:10px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}.journal-section ul{list-style:none;margin:0;padding:0;display:grid;gap:5px}.journal-entry{display:flex;gap:8px;align-items:flex-start;padding:7px 9px;border-radius:8px;background:var(--surface-2)}.journal-entry.muted{opacity:.62}.journal-body{display:grid;gap:2px;min-width:0}.journal-body strong{font-size:12px;overflow-wrap:anywhere}.journal-body p{margin:0;color:var(--ink-2);overflow-wrap:anywhere}.journal-body small{color:var(--muted);font-size:11px;overflow-wrap:anywhere}.journal-note{font-style:italic}.journal-chip{flex:0 0 auto;padding:2px 7px;border-radius:999px;font-size:10px;font-weight:800;background:var(--surface-3);color:var(--ink-2)}.journal-chip.error{background:var(--red-bg);color:var(--red-ink)}.journal-chip.warning{background:var(--amber-bg);color:var(--amber-ink)}.journal-chip.info{background:var(--blue-bg);color:var(--blue-ink)}.journal-outcome{justify-self:start;padding:1px 7px;border-radius:999px;font-size:10px;font-weight:750;background:var(--surface-3);color:var(--ink-2)}.journal-outcome.accepted,.journal-outcome.converted,.journal-outcome.applied,.journal-outcome.done,.journal-outcome.resolved,.journal-outcome.passed{background:var(--green-bg);color:var(--green)}.journal-outcome.rejected,.journal-outcome.failed,.journal-outcome.rolled_back,.journal-outcome.error{background:var(--red-bg);color:var(--red)}.journal-outcome.ready,.journal-outcome.warning,.journal-outcome.skipped{background:var(--amber-bg);color:var(--amber)}.journal-hidden{margin-top:5px}.journal-hidden summary{cursor:pointer;font-size:11px;color:var(--muted)}.journal-hidden ul{margin-top:5px}
</style>
