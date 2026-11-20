// Помощники для ключа ``ink_coverage`` DTO заказа (callas inkcoverage).
// ``percent`` в данных — среднее покрытие краской по рамке страницы, не TAC.

// «Светлые участки» — сплошные участки с суммой красок ниже порога (по цветоделениям),
// а не среднее по странице. «underflow» — флаг старых запусков (среднее ниже минимума).
const LABELS = { light_areas: "Светлые участки", overflow: "Перелив", underflow: "Низкая средняя красочность" };

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

// Главный флаг для пиктограммы: перелив важнее светлых участков.
export function inkCoverageMainFlag(data) {
  const flags = inkCoverageFlags(data);
  return flags.find((flag) => flag.kind === "overflow") || flags[0] || null;
}

export function inkCoverageFlagLabel(flag) {
  return flag ? LABELS[flag.kind] || "" : "";
}

// «Перелив: 262% на стр. 2 (порог 250%)»; «Светлые участки: 52% площади стр. 2 светлее 20%»
export function inkCoverageFlagTitle(flag) {
  if (!flag) return "";
  if (flag.kind === "light_areas") {
    return `${inkCoverageFlagLabel(flag)}: ${formatPercent(flag.percent)}% площади стр. ${flag.page} светлее ${formatPercent(flag.threshold)}%`;
  }
  const page = flag.page != null ? ` на стр. ${flag.page}` : "";
  const threshold = flag.threshold != null ? ` (порог ${formatPercent(flag.threshold)}%)` : "";
  return `${inkCoverageFlagLabel(flag)}: ${formatPercent(flag.percent)}%${page}${threshold}`;
}

// Подсказка для пиктограммы: все флаги через «; ».
export function inkCoverageTitle(data) {
  return inkCoverageFlags(data).map(inkCoverageFlagTitle).join("; ");
}

// Доля светлых участков страницы (``ink_coverage.light_areas``) или null.
export function lightAreaPercent(data, page) {
  const block = data?.light_areas;
  if (block?.status !== "completed" || !Array.isArray(block.pages)) return null;
  const entry = block.pages.find((item) => item?.page === page);
  return entry && Number.isFinite(entry.area_percent) ? entry.area_percent : null;
}
