// Допечатные проверки callas на карточке заказа (order.prepress, order.color_conversion).
// Данные считает бэкенд (services/callas/prepress.py); здесь только отображение.

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
};

export const CLIPPING_CODE = "action:2079";

const ok = (block) => block && block.status === "ok";

export function iccOff(order) {
  const info = order?.color_conversion;
  if (!info || info.status !== "converted" || !info.icc_removed) return null;
  const pages = (info.icc_pages || []).join(", ");
  return `ICC-профиль снят, цвет переведён в DeviceCMYK${pages ? ` (стр. ${pages})` : ""}. Исходный ICC/Lab больше не действует: проверьте цвета по превью.`;
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
  const groups = Array.isArray(order?.issue_groups) ? order.issue_groups : [];
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
    notices.push({ key: black.detected ? "black_solid" : "black_manual", level: black.detected ? "warning" : "info", text: black.message });
  }
  const clip = clippingGroup(order);
  if (clip) notices.push({ key: "clipping", level: "warning", text: `${clip.message}. Источник: Enfocus PitStop. Снятие обтравки может проявить скрытое содержимое.` });
  const fonts = fontsInfo(order);
  if (fonts && fonts.count > 0 && fonts.notEmbedded > 0) {
    notices.push({ key: "fonts_not_embedded", level: "warning", text: "Шрифты не встроены, перевод в кривые невозможен, нужна проверка." });
  }
  return notices;
}

export function availableFixes(order) {
  const fixes = [];
  const fonts = fontsInfo(order);
  if (fonts && fonts.count > fonts.notEmbedded) fixes.push("fonts_to_outlines");
  const black = order?.prepress?.black_solid;
  if (ok(black) && black.detected) fixes.push("black_to_rich");
  if (clippingGroup(order)) fixes.push("remove_clipping");
  return fixes;
}
