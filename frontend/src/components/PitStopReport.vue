<script setup>
import { computed, ref } from "vue";

const props = defineProps({ pitstop: { type: Object, required: true } });
const expanded = ref(false);
const counts = computed(() => props.pitstop.counts || {});
const issues = computed(() => props.pitstop.issues || []);
const executionStatus = computed(() => String(props.pitstop.execution_status || "").toLowerCase());
const verdict = computed(() => String(props.pitstop.verdict || "").toLowerCase());
const pending = computed(() => ["", "not_started", "queued", "pending", "running", "checking", "processing"].includes(executionStatus.value));
const technicalFailure = computed(() => ["failed", "error", "technical_error"].includes(executionStatus.value));
const pdfError = computed(() => !technicalFailure.value && ["error", "failed", "rejected"].includes(verdict.value));
const tone = computed(() => technicalFailure.value || pdfError.value ? "error" : pending.value ? "pending" : verdict.value === "warning" ? "warning" : "passed");
const title = computed(() => {
  if (technicalFailure.value) return "Технический сбой PitStop";
  if (pending.value) return "PitStop проверяет PDF";
  if (pdfError.value) return "PDF не прошёл PitStop";
  if (verdict.value === "warning") return "PitStop: есть предупреждения";
  return "PDF прошёл PitStop";
});
const reportUrls = computed(() => {
  const reports = props.pitstop.reports || {};
  return {
    json: reports.json_url || reports.json || props.pitstop.report_json_url || props.pitstop.json_url,
    xml: reports.xml_url || reports.xml || props.pitstop.report_xml_url || props.pitstop.xml_url,
  };
});
const checkedAt = computed(() => {
  if (!props.pitstop.checked_at) return "";
  const value = new Date(props.pitstop.checked_at);
  return Number.isNaN(value.getTime()) ? props.pitstop.checked_at : value.toLocaleString("ru-RU");
});
const profileLabel = computed(() => {
  const profile = props.pitstop.profile;
  if (!profile) return "—";
  if (typeof profile === "string") return profile;
  const name = profile.name || profile.key || "—";
  return profile.version ? `${name} · ${profile.version}` : name;
});
const issueText = (issue) => typeof issue === "string" ? issue : (issue.message || issue.code || "Проблема без описания");
const issueKey = (issue, index) => typeof issue === "string" ? `${index}:${issue}` : (issue.id || `${issue.action_id || "issue"}:${issue.page || 0}:${index}`);
const issuePage = (issue) => typeof issue === "object" && issue ? (issue.page ?? issue.locations?.[0]?.page) : null;
</script>

<template>
  <section class="pitstop-report" :class="`pitstop-${tone}`" :aria-label="title">
    <header class="pitstop-head">
      <div>
        <p class="section-title">Проверка финального PDF</p>
        <strong>{{ title }}</strong>
      </div>
      <span class="pitstop-mark" aria-hidden="true">{{ pending ? "…" : (technicalFailure || pdfError ? "!" : "✓") }}</span>
    </header>

    <p v-if="technicalFailure" class="pitstop-technical-note">PDF не признан бракованным: сервис проверки не завершил работу. Печать заблокирована до повторной проверки.<span v-if="pitstop.technical_error"> Причина: {{ pitstop.technical_error }}</span></p>
    <dl class="pitstop-meta">
      <div><dt>Профиль</dt><dd>{{ profileLabel }}</dd></div>
      <div><dt>Страниц</dt><dd>{{ pitstop.pages ?? "—" }}</dd></div>
      <div><dt>Ошибок</dt><dd>{{ counts.errors ?? 0 }}</dd></div>
      <div><dt>Предупреждений</dt><dd>{{ counts.warnings ?? 0 }}</dd></div>
      <div v-if="counts.critical_failures"><dt>Критических сбоев</dt><dd>{{ counts.critical_failures }}</dd></div>
      <div v-if="counts.noncritical_failures"><dt>Некритических сбоев</dt><dd>{{ counts.noncritical_failures }}</dd></div>
      <div v-if="counts.fixes"><dt>Исправлений</dt><dd>{{ counts.fixes }}</dd></div>
      <div v-if="checkedAt"><dt>Проверено</dt><dd>{{ checkedAt }}</dd></div>
    </dl>

    <button v-if="issues.length" type="button" class="pitstop-toggle" @click="expanded = !expanded">
      {{ expanded ? "Скрыть проблемы" : `Показать проблемы (${issues.length})` }}
    </button>
    <ul v-if="expanded" class="pitstop-issues">
      <li v-for="(issue, index) in issues" :key="issueKey(issue, index)" :class="`issue-${issue.severity || 'info'}`">
        <span>{{ issueText(issue) }}</span><small v-if="issuePage(issue)">Страница {{ issuePage(issue) }}</small>
      </li>
    </ul>

    <footer v-if="reportUrls.json || reportUrls.xml" class="pitstop-links">
      <a v-if="reportUrls.json" :href="reportUrls.json" target="_blank" rel="noopener">Отчёт JSON ↗</a>
      <a v-if="reportUrls.xml" :href="reportUrls.xml" target="_blank" rel="noopener">Отчёт XML ↗</a>
    </footer>
  </section>
</template>
