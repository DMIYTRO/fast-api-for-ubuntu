// Журнал заказа (GET /api/order-history/{id}/journal, схема image-magic.order-journal v1).
// Данные собирает бэкенд (services/order_journal.py); здесь только группировка и подписи.

export const JOURNAL_SECTIONS = [
  { kind: "issue", title: "Найдено в файлах" },
  { kind: "auto_change", title: "Исправлено автоматически" },
  { kind: "operator_change", title: "Действия оператора" },
  { kind: "decision", title: "Решение" },
];

export const SEVERITY_LABELS = { error: "Ошибка", warning: "Предупреждение", info: "Инфо" };

export const OUTCOME_LABELS = {
  resolved: "Устранено",
  accepted: "Принято",
  rejected: "Отклонено",
  failed: "Не выполнено",
  converted: "Выполнено",
  applied: "Применено",
  skipped: "Пропущено",
  done: "Выполнено",
  rolled_back: "Отменено",
  reset: "Сброшено",
  ready: "Ожидает решения",
  processing: "Выполняется",
  passed: "Готов",
  warning: "Требует доработки",
  error: "Ошибка",
};

// Находка, добавленная исправлением Callas (оператору в карточке не показывается).
export const isAddedByFix = (entry) => entry?.data?.added_by_fix === true;

// Приглушённые записи: понижённые политикой, добавленные исправлением, устранённые.
export function isMuted(entry) {
  return Boolean(entry?.data?.policy_demoted || isAddedByFix(entry) || entry?.outcome === "resolved");
}

export function entryNotes(entry) {
  const notes = [];
  if (entry?.data?.policy_demoted) notes.push("Не влияет на статус (облегчённая проверка)");
  if (isAddedByFix(entry)) notes.push("Добавлено исправлением Callas: в карточке скрыто");
  else if (entry?.data?.introduced_by_fix) notes.push(`Исправление Callas добавило ещё ${entry.data.introduced_by_fix}`);
  if (entry?.data?.policy_unclassified) notes.push("Новая проверка PitStop без категории");
  return notes;
}

// Секции с записями: { kind, title, entries, hidden } — hidden: добавленные исправлением находки.
export function groupJournal(journal) {
  const entries = Array.isArray(journal?.entries) ? journal.entries.filter((entry) => entry && typeof entry === "object") : [];
  return JOURNAL_SECTIONS.map((section) => {
    const items = entries.filter((entry) => entry.kind === section.kind);
    return {
      ...section,
      entries: items.filter((entry) => !isAddedByFix(entry)),
      hidden: items.filter(isAddedByFix),
    };
  }).filter((section) => section.entries.length || section.hidden.length);
}

export function entryMeta(entry, formatDate) {
  const parts = [];
  if (entry?.file) parts.push(entry.file);
  if (Array.isArray(entry?.pages) && entry.pages.length) parts.push(`стр. ${entry.pages.join(", ")}`);
  if (Number.isInteger(entry?.count) && entry.count > 1) parts.push(`×${entry.count}`);
  if (entry?.at && formatDate) parts.push(formatDate(entry.at));
  return parts.join(" · ");
}
