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

// Кнопка «в составной чёрный»: есть чистый K100 (плашка или малая доля площади). Если детектор не отработал
// (службе callas не включены цветоделения, сбой, проверка выключена), оператору всё равно даём запустить
// профиль: он сам сообщит, что K100 не найден (fixups_applied == 0), а лишнего не изменит.
export const UNCONFIRMED_NOTE = "Проверка K100 недоступна — кнопка показана без подтверждения наличия плашки";

export function blackFixAvailable(black) {
  if (!black || typeof black !== "object") return false;
  if (ok(black)) return Boolean(black.detected || black.fix_available);
  return black.status === "failed" || black.status === "skipped";
}

// Кнопки, показанные без подтверждения проверкой: сейчас только «в составной чёрный» при недоступном детекторе K100.
export function unconfirmedFixes(order) {
  const black = order?.prepress?.black_solid;
  return blackFixAvailable(black) && !ok(black) ? ["black_to_rich"] : [];
}

export function availableFixes(order) {
  const fixes = [];
  const fonts = fontsInfo(order);
  if (fonts && fonts.count > fonts.notEmbedded) fixes.push("fonts_to_outlines");
  const black = order?.prepress?.black_solid;
  if (blackFixAvailable(black)) fixes.push("black_to_rich");
  if (clippingGroup(order)) fixes.push("remove_clipping");
  return fixes;
}
