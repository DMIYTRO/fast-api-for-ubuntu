// Группировка однотипных проблем. Зеркало services/issue_groups.py:
// бэкенд отдаёт готовый order.issue_groups, а для старых payload без него
// фронтенд строит группы сам по тем же правилам (числа схлопываются).

const SAMPLE_LIMIT = 3;
const NUMBER_RE = /\d+(?:[.,]\d+)?/g;

const LOCAL_RULES = [
  ["size_corrected", /^размер автоматически скорректирован/i, "Размер автоматически скорректирован"],
  ["size_confirmation_required", /^требуется подтверждение коррекции/i, "Требуется подтверждение коррекции размера"],
  ["size_mismatch", /^размер \d/i, "Размер не соответствует ожидаемому"],
  ["file_too_large", /^размер файла .*превышает лимит/i, "Размер файла превышает лимит"],
  ["correction_rejected", /отказался от предложенной коррекции/i, "Коррекция размера отклонена пользователем"],
  ["correction_confirmed", /^коррекция размера подтверждена/i, "Коррекция размера подтверждена пользователем"],
  ["low_dpi", /^разрешение .*DPI/i, "Низкое разрешение"],
  ["colorspace_not_allowed", /^цветовая модель/i, "Цветовая модель не входит в профиль"],
  ["colors_not_supported", /^цветность .*не поддерживается/i, "Цветность не поддерживается"],
  ["unsupported_format", /^формат .*исключён из обработки/i, "Формат файла исключён из обработки"],
  ["file_unreadable", /^не удалось прочитать файл/i, "Не удалось прочитать файл"],
  ["multipage_file", /^файл содержит \d+ страниц\/изображений/i, "Многостраничный файл; разрешён только одностраничный"],
  ["tiff_alpha", /^TIFF содержит альфа-канал/i, "TIFF содержит альфа-канал"],
  ["tiff_layers", /несведённ(ых|ые|ый) сло/i, "TIFF содержит несведённые слои"],
  ["tiff_pages", /^TIFF содержит \d+ страниц/i, "Многостраничный TIFF; разрешён только одностраничный"],
  ["orientation_mismatch", /^ориентация сторон не совпадает/i, "Ориентация сторон не совпадает"],
  ["auto_rotate", /будет автоматически повёрнут/i, "Сторона будет автоматически повёрнута"],
  ["pdf_page_size", /^PDF страница \d+ имеет размер/i, "Размер страницы PDF не соответствует ожидаемому"],
  ["pdf_page_rotation", /^PDF страница \d+ имеет недопустимый поворот/i, "Недопустимый поворот страницы PDF"],
  ["pdf_unknown_colorspace", /^PDF страница \d+: неизвестная цветовая модель/i, "Неизвестная цветовая модель на странице PDF"],
  ["pdf_rgb_raster", /^PDF страница \d+: растровое изображение/i, "Растровое изображение в PDF имеет RGB colorspace"],
  ["pdf_low_dpi", /^PDF страница \d+: effective DPI/i, "Низкое эффективное разрешение в PDF"],
  ["pdf_too_many_pages", /^PDF contains \d+ pages/i, "В PDF слишком много страниц"],
  ["pdf_no_pages", /^PDF contains no pages/i, "PDF не содержит страниц"],
  ["pdf_encrypted", /^Encrypted PDF/i, "Зашифрованный PDF не поддерживается"],
  ["pdf_repaired", /^PDF required structural repair/i, "PDF потребовал структурного восстановления"],
  ["pdf_page_uninspectable", /^Page \d+ cannot be inspected/i, "Страницу PDF не удалось проверить"],
  ["pdf_error", /^PDF: /i, "Ошибка обработки PDF"],
  ["duplex_pages_required", /^для цветности с оборотом требуется/i, "Неверное число страниц для двусторонней печати"],
  ["two_page_pdf_one_sided", /^двухстраничный PDF недопустим/i, "Двухстраничный PDF недопустим для односторонней печати"],
  ["two_page_pdf_extra_sides", /^двухстраничный PDF является полным заказом/i, "Лишние стороны при двухстраничном PDF"],
  ["multiple_two_page_pdf", /^найдено несколько двухстраничных PDF/i, "Несколько двухстраничных PDF в заказе"],
  ["missing_face", /^не найден обязательный файл face/i, "Не найден файл face"],
  ["missing_back", /^для двусторонней печати не найден файл back/i, "Не найден файл back"],
  ["extra_back", /^для односторонней печати найден лишний файл back/i, "Лишний файл back"],
  ["single_file_required", /^для односторонней печати разрешён только один/i, "Для односторонней печати разрешён один файл"],
  ["multiple_face", /^найдено несколько файлов face/i, "Несколько файлов face"],
  ["multiple_back", /^найдено несколько файлов back/i, "Несколько файлов back"],
  ["customer_mismatch", /не совпадает номер клиента/i, "Номер клиента в именах файлов не совпадает"],
  ["name_size_mismatch", /не совпадает размер в имени/i, "Размер в именах файлов не совпадает"],
  ["name_colors_mismatch", /не совпадает цветность в имени/i, "Цветность в именах файлов не совпадает"],
  ["mixed_formats", /^стороны заказа имеют разные форматы/i, "Стороны заказа имеют разные форматы"],
  ["preview_error", /^(Превью|Производственное превью)/i, "Ошибка создания превью"],
  ["processing_timeout", /^Превышено время обработки/i, "Превышено время обработки заказа"],
  ["pdf_build_missing", /^BatchProcessor не вернул/i, "Обработчик не вернул результат"],
];

const PITSTOP_SEVERITY = { critical_failure: "error", error: "error", noncritical_failure: "warning", warning: "warning" };

export const issueText = (value) => {
  if (value == null) return "";
  if (typeof value === "string") return value.trim();
  return String(value.message || value.code || "").trim();
};

export function normalizeIssueMessage(message) {
  const text = String(message || "").replace(/\s+/g, " ").trim();
  for (const [code, pattern, label] of LOCAL_RULES) {
    if (pattern.test(text)) return { code, label };
  }
  const collapsed = text.replace(NUMBER_RE, "#").replace(/\s+/g, " ").trim();
  return { code: collapsed || "empty", label: text };
}

export function pitstopSeverity(value) {
  return PITSTOP_SEVERITY[String(value || "").toLowerCase()] || "info";
}

export function pluralRu(count, one, few, many) {
  const rem10 = count % 10;
  const rem100 = count % 100;
  if (rem10 === 1 && rem100 !== 11) return one;
  if (rem10 >= 2 && rem10 <= 4 && (rem100 < 12 || rem100 > 14)) return few;
  return many;
}

export function issueObjectLabel(count) {
  return pluralRu(count, "объект", "объекта", "объектов");
}

export function formatGroupMessage(base, { count = 1, objects = null, pages = [], files = [] } = {}) {
  const parts = [];
  if (objects != null) parts.push(`${objects} ${issueObjectLabel(objects)}`);
  else if (count > 1) parts.push(`${count} ${pluralRu(count, "повтор", "повтора", "повторов")}`);
  if (pages.length) parts.push(`на ${pluralRu(pages.length, "странице", "страницах", "страницах")} ${pages.join(", ")}`);
  if (files.length > 1) parts.push(`в ${files.length} ${pluralRu(files.length, "файле", "файлах", "файлах")}`);
  else if (files.length === 1 && count > 1) parts.push(`в файле ${files[0]}`);
  return parts.length ? `${base} — ${parts.join(" ")}` : base;
}

class GroupBuilder {
  constructor() { this.groups = new Map(); }

  add({ source, severity, code, label, message, file = null, locations = [] }) {
    const key = `${source}\u0000${severity}\u0000${code}`;
    let group = this.groups.get(key);
    if (!group) {
      group = { code, severity, source, label, count: 0, objectKeys: new Set(), pages: new Set(), files: [], samples: [], hasLocations: false, unknownObjects: false };
      this.groups.set(key, group);
    }
    group.count += 1;
    if (message && group.samples.length < SAMPLE_LIMIT && !group.samples.includes(message)) group.samples.push(message);
    if (file && !group.files.includes(file)) group.files.push(file);
    if (!locations.length) group.unknownObjects = true;
    for (const location of locations) {
      if (!location || typeof location !== "object") continue;
      const page = Number.isInteger(location.page) ? location.page : null;
      if (page == null) { group.unknownObjects = true; continue; }
      group.hasLocations = true;
      group.pages.add(page);
      const bbox = location.bbox || {};
      if (["min_x", "min_y", "max_x", "max_y"].some((key) => bbox[key] == null)) { group.unknownObjects = true; continue; }
      group.objectKeys.add([page, bbox.min_x, bbox.min_y, bbox.max_x, bbox.max_y].join("|"));
    }
  }

  build() {
    const order = { error: 0, warning: 1, info: 2 };
    return [...this.groups.values()]
      .sort((a, b) => (order[a.severity] ?? 3) - (order[b.severity] ?? 3) || (a.source === "local" ? 0 : 1) - (b.source === "local" ? 0 : 1))
      .map((group) => {
        const objects = group.hasLocations && !group.unknownObjects ? group.objectKeys.size : null;
        const pages = [...group.pages].sort((a, b) => a - b);
        const base = group.count === 1 && group.samples.length ? group.samples[0] : group.label;
        return {
          code: group.code,
          severity: group.severity,
          source: group.source,
          message: formatGroupMessage(base, { count: group.count, objects, pages, files: group.files }),
          count: group.count,
          objects,
          pages,
          files: [...group.files],
          samples: [...group.samples],
        };
      });
  }
}

function addPitStopIssues(builder, issues) {
  for (const issue of issues || []) {
    const message = issueText(issue) || "PitStop: сообщение отсутствует";
    const severity = typeof issue === "object" && issue ? pitstopSeverity(issue.severity) : "info";
    const actionId = typeof issue === "object" && issue ? issue.action_id : null;
    const code = actionId != null && actionId !== "" ? `action:${actionId}` : message.replace(NUMBER_RE, "#");
    let locations = typeof issue === "object" && issue ? issue.locations : null;
    if ((!locations || !locations.length) && typeof issue === "object" && issue && Number.isInteger(issue.page)) locations = [{ page: issue.page }];
    builder.add({ source: "pitstop", severity, code, label: message, message, locations: locations || [] });
  }
}

export function groupPitStopIssues(issues) {
  const builder = new GroupBuilder();
  addPitStopIssues(builder, issues);
  return builder.build();
}

export function buildIssueGroups(order) {
  const builder = new GroupBuilder();
  const local = (severity, value, file = null) => {
    const message = issueText(value);
    if (!message) return;
    const { code, label } = normalizeIssueMessage(message);
    builder.add({ source: "local", severity, code, label, message, file });
  };
  const revision = order?.revision_check;
  const errors = revision ? revision.errors || [] : order?.errors || [];
  errors.forEach((item) => local("error", item));
  (order?.processing_errors || []).filter((item) => !revision || !errors.includes(item)).forEach((item) => local("error", item));
  (revision ? revision.warnings || [] : order?.warnings || []).forEach((item) => local("warning", item));
  for (const file of revision ? [] : order?.files || []) {
    const name = file.filename || file.name || (file.path ? String(file.path).split("/").pop() : null);
    (file.errors || []).forEach((item) => local("error", item, name));
    (file.warnings || []).forEach((item) => local("warning", item, name));
  }
  addPitStopIssues(builder, order?.pitstop?.issues);
  return builder.build();
}

// Предпочитает готовые группы бэкенда; старые payload группируются на месте.
export function orderIssueGroups(order) {
  if (Array.isArray(order?.issue_groups)) return order.issue_groups;
  return buildIssueGroups(order);
}

// Совместимость: группировка плоского списка сообщений по типу проблемы.
export function groupIssueMessages(messages) {
  const groups = new Map();
  for (const value of messages || []) {
    const message = issueText(value);
    if (!message) continue;
    const { code, label } = normalizeIssueMessage(message);
    const group = groups.get(code) || { first: message, label, count: 0 };
    group.count += 1;
    groups.set(code, group);
  }
  return [...groups.values()].map(({ first, label, count }) => ({ message: count === 1 ? first : label, count }));
}
