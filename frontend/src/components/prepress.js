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
};

export const CLIPPING_CODE = "action:2079";

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

export function clippingGroup(order) {
  const groups = visibleIssueGroups(Array.isArray(order?.issue_groups) ? order.issue_groups : [], order?.pitstop);
  return groups.find((group) => group.code === CLIPPING_CODE || group.fix_plan === "remove_clipping") || null;
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
  if (clip) notices.push({ key: "clipping", level: "warning", text: `${clip.message}. Источник: Enfocus PitStop. Снятие обтравки может проявить скрытое содержимое.` });
  const fonts = fontsInfo(order);
  if (fonts && fonts.count > 0 && fonts.notEmbedded > 0) {
    notices.push({ key: "fonts_not_embedded", level: "warning", text: "Шрифты не встроены, перевод в кривые невозможен, нужна проверка." });
  }
  return notices;
}

// Кнопки исправлений Callas показываются всегда (при наличии PDF) и всегда активны.
// Проверка только подсвечивает «рекомендованные» — те, где она нашла что исправлять.
// Если исправлять нечего, профиль сам сообщит об этом (fixups_applied == 0) и ничего не изменит.
export const ALL_FIXES = ["fonts_to_outlines", "black_to_rich", "remove_clipping", "downsample_images"];
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
  return fixes;
}

export function availableFixes() {
  return [...ALL_FIXES];
}
