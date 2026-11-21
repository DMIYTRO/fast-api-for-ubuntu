// Допечатные проверки callas на карточке заказа (order.prepress, order.color_conversion).
// Данные считает бэкенд (services/callas/prepress.py); здесь только отображение.

import { visibleIssueGroups } from "./pitstopBaseline.js";

export const FIX_BUTTONS = {
  fonts_to_outlines: {
    label: "Перевести шрифты в кривые",
    title: "Callas: Fixup «Convert fonts to outlines». Встроенные шрифты станут контурами; исходный PDF сохраняется, результат нужно принять или отклонить. После кривых текст нельзя скопировать или найти поиском. Невстроенные шрифты (Helvetica, Courier) профиль не переводит.",
  },
  black_to_rich: {
    label: "Перевести плашку в составной чёрный",
    title: "Callas: Fixup «Map colors» K100 → C60 M40 Y40 K100. Профиль меняет ВСЕ объекты с чистым K100, не только плашку: мелкий текст и штрихкоды рискуют потерять резкость (совмещение красок). Проверьте наложение перед принятием.",
  },
  remove_clipping: {
    label: "Удалить обтравку",
    title: "Callas: Fixup «RemoveClipPathes» удалит все обтравки, в том числе служебную обтравку по странице (замечание «Clipping objects found» создаёт Enfocus PitStop, ActionID 2079). Снятие масок может проявить скрытое содержимое: сравните «до» и «после».",
  },
  downsample_images: {
    label: "Уменьшить разрешение до 300 ppi",
    title: "Callas: Fixup «Downsample image resolution to specified value»: уменьшит до 300 ppi все изображения выше 300 ppi; штриховые 1-бит проверьте по превью. Исходный PDF сохраняется, результат нужно принять или отклонить.",
  },
  reduce_tac: {
    label: "Снизить покрытие до 300%",
    title: "Callas: Fixup «TAC 300 - CMYK and ICC CMYK - preserve spots» (DeviceLink ISO Coated v2 TAC 300) пересчитает объекты CMYK и ICC-CMYK так, чтобы сумма красок не превышала 300% (замечание «Total ink coverage» создаёт Enfocus PitStop, ActionID 1153). Плашечные цвета и RGB не меняются. Пересчитываются ВСЕ CMYK-цвета: тёмные и насыщенные места, составной чёрный и тёмные фото немного изменятся. Исходный PDF сохраняется, сравните «до» и «после» перед принятием.",
  },
};

export const CLIPPING_CODE = "action:2079";
// «Total ink coverage of an object is more than 310%» (Enfocus PitStop).
export const INK_COVERAGE_CODE = "action:1153";

const ok = (block) => block && block.status === "ok";

// Значок «Поля Callas»: исходный файл заказа заменён принятым исправлением Callas
// (file.callas_source_fix.status === "accepted"). Это пометка, а не предупреждение.
export function callasSourceFixTitle(order) {
  const files = order?.files || order?.file_results || [];
  const parts = files
    .filter((file) => file?.callas_source_fix?.status === "accepted")
    .map((file) => {
      const fix = file.callas_source_fix;
      const name = file.name || file.filename || String(file.path || "").split("/").pop() || "файл";
      if (fix.plan === "size_correction") {
        return `${name}: ${fix.bleed_only ? "Callas дорисовал поля вылета, макет не изменён" : "размер исправлен Callas, макет изменён"}`;
      }
      return `${name}: исправлено Callas («${fix.plan_title || fix.plan || "исправление"}»)`;
    });
  return parts.join("; ");
}

const SIZE_FIX_LABELS = {
  bleed: "Поля Callas",
  reveal: "Поля открыты",
  stretch: "Растянуто",
  shrink: "Уменьшено",
  fill: "Подогнано",
  // Файл с меткой (SRA3)/(SRA3P)/(BAN) в имени: без масштабирования — белые поля или обрезка.
  canvas: "Поля/обрезка",
  callas: "Исправлено Callas",
};
const SIZE_FIX_MIXED_LABEL = "Размер скорректирован";

function fileName(file) {
  return file.name || file.filename || String(file.path || "").split("/").pop() || "файл";
}

function formatMm(value) {
  const number = Number(value);
  return Number.isFinite(number) ? String(Math.round(number * 100) / 100) : "?";
}

function formatSize(size) {
  return Array.isArray(size) && size.length >= 2 ? `${formatMm(size[0])}×${formatMm(size[1])}` : "";
}

function fileSizeFixPart(file) {
  const fix = file.size_fix;
  const callas = file.callas_source_fix?.status === "accepted"
    ? callasSourceFixTitle({ files: [file] }).replace(`${fileName(file)}: `, "")
    : "";
  if (!fix?.method) return callas ? `${fileName(file)}: ${callas}` : "";
  const sizes = [formatSize(fix.from_mm), formatSize(fix.to_mm)].filter(Boolean);
  const detail = fix.detail || SIZE_FIX_LABELS[fix.method] || "размер скорректирован";
  const range = sizes.length === 2 ? `, ${sizes[0]}→${sizes[1]} мм` : "";
  const mode = fix.auto === false ? "подтверждено" : "автоматически";
  return `${fileName(file)}: ${detail}${range} (${mode})${callas ? `; ${callas}` : ""}`;
}

// Единственный значок размера на заказ: объединяет file.size_fix (лестница коррекции размера)
// и принятое исправление Callas (callas_source_fix). Принятое вручную исправление без size_fix
// по-прежнему даёт «Поля Callas». Возвращает { label, title } или null.
export function sizeFixBadge(order) {
  const files = order?.files || order?.file_results || [];
  const methods = new Set();
  const parts = [];
  for (const file of files) {
    if (!file) continue;
    const accepted = file.callas_source_fix?.status === "accepted" ? file.callas_source_fix : null;
    // Принятое исправление другого плана (не размера) — просто «Callas».
    const method = file.size_fix?.method
      || (accepted ? (accepted.plan === "size_correction" ? "bleed" : "callas") : null);
    if (!method) continue;
    methods.add(method);
    const part = fileSizeFixPart(file);
    if (part) parts.push(part);
  }
  if (!methods.size) return null;
  const label = methods.size === 1 ? (SIZE_FIX_LABELS[[...methods][0]] || SIZE_FIX_MIXED_LABEL) : SIZE_FIX_MIXED_LABEL;
  return { label, title: parts.join("; ") };
}

// Значок «ICC off»: бэкенд ставит icc_removed только если ICC/выходной профиль был и из результата
// исчез. Неудавшаяся конвертация оставляет PDF как был, поэтому для неё значка нет.
export function iccOff(order) {
  const info = order?.color_conversion;
  if (!info || info.status === "failed" || info.icc_removed !== true) return null;
  const iccPages = info.icc_pages || [];
  const remaining = new Set(info.icc_remaining_pages || []);
  const removedPages = iccPages.filter((page) => !remaining.has(page)).join(", ");
  const parts = [];
  if (removedPages || !info.output_intent_removed) {
    parts.push(`ICC-профиль снят, цвет переведён в DeviceCMYK${removedPages ? ` (стр. ${removedPages})` : ""}`);
  }
  if (info.output_intent_removed) parts.push("выходной профиль (OutputIntent) удалён");
  if (remaining.size) parts.push(`ICC остался на стр. ${[...remaining].join(", ")}`);
  return `${parts.join("; ")}. Исходный ICC/Lab больше не действует: проверьте цвета по превью.`;
}

// Политика проверки заказа (order.check_policy: { key, label, reason }); у старых запусков её нет.
export function checkPolicyBadge(order) {
  const policy = order?.check_policy;
  if (!policy || typeof policy !== "object" || !policy.label) return null;
  const basic = policy.key === "basic";
  const title = basic
    ? "Файлы из папки Input: замечания PitStop вроде прозрачности и обтравки записаны как информация и не влияют на статус"
    : "Полная проверка: все замечания PitStop влияют на статус";
  return { key: basic ? "basic" : "full", label: policy.label, title };
}

// Автоматическая коррекция цвета (order.color_conversion): модель RGB → CMYK и/или снятие ICC.
// reason отсутствует у старых записей: это RGB. Возвращает строки допечатных замечаний.
export function colorConversionNotes(order) {
  const info = order?.color_conversion;
  if (!info || typeof info !== "object") return [];
  const iccReason = info.reason === "icc";
  const pages = (info.pages || []).join(", ");
  const where = pages ? ` (стр. ${pages})` : "";
  if (info.status === "converted") {
    if (iccReason) {
      return [{ key: "color_icc", level: "info", text: `Цветовой профиль: ICC снят (CMYK без пересчёта)${where}` }];
    }
    const notes = [{ key: "color_rgb", level: "info", text: `Цветовая модель: RGB → CMYK скорректирована${where}` }];
    if (info.icc_removed === true) notes.push({ key: "color_icc", level: "info", text: "Цветовой профиль: ICC снят" });
    return notes;
  }
  if (info.status === "failed") {
    const reason = info.error ? `: ${info.error}` : "";
    return [iccReason
      ? { key: "color_icc_failed", level: "warning", text: `Не удалось снять ICC-профиль${reason}` }
      : { key: "color_rgb_failed", level: "warning", text: `Не удалось перевести RGB в CMYK${reason}` }];
  }
  if (info.status === "skipped" && info.skip_message) {
    return [{ key: "color_skipped", level: "info", text: `Цветовой профиль: ${info.skip_message}` }];
  }
  return [];
}

export function fontsInfo(order) {
  const block = order?.prepress?.fonts;
  if (!ok(block)) return null;
  return {
    count: block.count || 0,
    fonts: block.fonts || [],
    notEmbedded: block.not_embedded || 0,
    converted: Boolean(block.converted) && !block.count,
  };
}

export function fontLabel(font) {
  const flags = [font.type];
  if (font.embedded === false) flags.push("не встроен");
  else if (font.embedded === true) flags.push("встроен");
  if (font.invisible_only) flags.push("невидимый");
  return `${font.name} (${flags.join(", ")})`;
}

function visibleGroup(order, code, plan) {
  const groups = visibleIssueGroups(Array.isArray(order?.issue_groups) ? order.issue_groups : [], order?.pitstop);
  return groups.find((group) => group.code === code || group.fix_plan === plan) || null;
}

export function clippingGroup(order) {
  return visibleGroup(order, CLIPPING_CODE, "remove_clipping");
}

// Группа PitStop о суммарном покрытии; скрытые сравнением с исходником находки не считаются.
export function inkCoverageGroup(order) {
  return visibleGroup(order, INK_COVERAGE_CODE, "reduce_tac");
}

// Справочные строки (уровень info: понижённая обтравка, «RGB → CMYK скорректирована», «ICC снят»…)
// в карточке пока не показываются: серый блок «Допечатная обработка» дублирует отчёт PitStop,
// а исправления цвета видны по значкам RGB→CMYK / ICC off. Вернуть — showInfoNotes: true.
export const prepressDisplay = { showInfoNotes: false };

// Строки блока допечатных замечаний в карточке: без справочных, пока они скрыты.
export function visiblePrepressNotices(order) {
  const notices = prepressNotices(order);
  return prepressDisplay.showInfoNotes ? notices : notices.filter((note) => note.level === "warning");
}

// Сгруппированные допечатные замечания (по одному на тип) и доступные исправления.
export function prepressNotices(order) {
  const prepress = order?.prepress || {};
  const notices = [];
  const ppi = prepress.high_ppi;
  if (ok(ppi) && ppi.message) notices.push({ key: "high_ppi", level: "warning", text: ppi.message });
  if (ok(ppi) && ppi.one_bit_message) notices.push({ key: "one_bit_ppi", level: "info", text: ppi.one_bit_message });
  const black = prepress.black_solid;
  if (ok(black) && black.message) {
    const key = black.detected ? "black_solid" : black.fix_available ? "black_solid_minor" : "black_manual";
    notices.push({ key, level: black.detected ? "warning" : "info", text: black.message });
  }
  const clip = clippingGroup(order);
  if (clip) {
    // Понижено политикой облегчённой проверки: информация, статус заказа не меняется.
    const demoted = clip.policy_demoted === true;
    notices.push({
      key: "clipping",
      level: demoted ? "info" : "warning",
      text: `${clip.message}. Источник: Enfocus PitStop.${demoted ? " Не влияет на статус (облегчённая проверка)." : ""} Снятие обтравки может проявить скрытое содержимое.`,
    });
  }
  notices.push(...colorConversionNotes(order));
  const fonts = fontsInfo(order);
  if (fonts && fonts.count > 0 && fonts.notEmbedded > 0) {
    notices.push({ key: "fonts_not_embedded", level: "warning", text: "Шрифты не встроены, перевод в кривые невозможен, нужна проверка." });
  }
  return notices;
}

// Кнопки исправлений Callas показываются всегда (при наличии PDF) и всегда активны.
// Проверка только подсвечивает «рекомендованные» — те, где она нашла что исправлять.
// Если исправлять нечего, профиль сам сообщит об этом (fixups_applied == 0) и ничего не изменит.
export const ALL_FIXES = ["fonts_to_outlines", "black_to_rich", "remove_clipping", "downsample_images", "reduce_tac"];
export const NOT_DETECTED_NOTE = "Проверка не нашла, что исправлять: профиль можно запустить, лишнего он не изменит";

export function blackFixAvailable(black) {
  if (!black || typeof black !== "object") return false;
  if (ok(black)) return Boolean(black.detected || black.fix_available);
  return false;
}

export function recommendedFixes(order) {
  const fixes = [];
  const fonts = fontsInfo(order);
  if (fonts && fonts.count > fonts.notEmbedded) fixes.push("fonts_to_outlines");
  if (blackFixAvailable(order?.prepress?.black_solid)) fixes.push("black_to_rich");
  if (clippingGroup(order)) fixes.push("remove_clipping");
  const ppi = order?.prepress?.high_ppi;
  if (ok(ppi) && ppi.count > 0) fixes.push("downsample_images");
  if (inkCoverageGroup(order)) fixes.push("reduce_tac");
  return fixes;
}

export function availableFixes() {
  return [...ALL_FIXES];
}
