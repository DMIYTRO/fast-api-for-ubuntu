// Помощники для ключа ``ink_coverage`` DTO заказа (callas inkcoverage).
// ``percent`` в данных — среднее покрытие краской по рамке страницы, не TAC.

const LABELS = { underflow: "Недолив", overflow: "Перелив" };

const formatPercent = (value) => {
  if (value == null) return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return "—";
  return Math.abs(parsed - Math.round(parsed)) < 0.05 ? String(Math.round(parsed)) : parsed.toFixed(1);
};

export function inkCoverageFlags(data) {
  return data?.status === "completed" && Array.isArray(data?.flags)
    ? data.flags.filter((flag) => flag && LABELS[flag.kind] && Number.isFinite(flag.percent) && Number.isFinite(flag.threshold) && Number.isInteger(flag.page) && flag.page > 0)
    : [];
}

// Главный флаг для пиктограммы: перелив важнее недолива.
export function inkCoverageMainFlag(data) {
  const flags = inkCoverageFlags(data);
  return flags.find((flag) => flag.kind === "overflow") || flags[0] || null;
}

export function inkCoverageFlagLabel(flag) {
  return flag ? LABELS[flag.kind] || "" : "";
}

// «Перелив: 262% на стр. 2 (порог 250%)»
export function inkCoverageFlagTitle(flag) {
  if (!flag) return "";
  const page = flag.page != null ? ` на стр. ${flag.page}` : "";
  const threshold = flag.threshold != null ? ` (порог ${formatPercent(flag.threshold)}%)` : "";
  return `${inkCoverageFlagLabel(flag)}: ${formatPercent(flag.percent)}%${page}${threshold}`;
}

// Подсказка для пиктограммы: все флаги через «; ».
export function inkCoverageTitle(data) {
  return inkCoverageFlags(data).map(inkCoverageFlagTitle).join("; ");
}
