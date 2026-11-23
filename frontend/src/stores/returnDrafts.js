import { reactive, toRaw } from "vue";

// Черновики причин возврата по заказу (ключ — orderIdentity). Живут вне карточки,
// чтобы перемонтирование (страница, фильтр) не теряло выбор и не возвращало
// снятые оператором предложения.
// Предложения проверки отмечаются автоматически только у заказов, которые
// возвращают: красных и жёлтых.
export const PRESELECT_STATUSES = ["error", "warning"];

export const CONFIDENCE_LABELS = { high: "точно", medium: "возможно", low: "на усмотрение" };

export const returnSuggestions = (order) =>
  (Array.isArray(order?.return_suggestions) ? order.return_suggestions : [])
    .filter((item) => item && typeof item.text === "string" && item.text.trim());

export const preselectedReasons = (order) => {
  if (!PRESELECT_STATUSES.includes(order?.status)) return [];
  return [...new Set(returnSuggestions(order).filter((item) => item.preselect === true).map((item) => item.text))];
};

/**
 * Строки комментария клиенту в том виде, в котором они уйдут: каждая с исходной
 * причиной (`source`) и признаком своего комментария (`custom`). Одна причина —
 * без номера, несколько — нумерованный список.
 */
export function returnCommentLines(selected, custom = "") {
  const customText = String(custom || "").trim();
  const parts = [
    ...(selected || []).filter(Boolean).map((source) => ({ source, custom: false })),
    ...(customText ? [{ source: customText, custom: true }] : []),
  ];
  return parts.map((part, index) => {
    const body = part.source.replace(/[.;\s]+$/, "");
    const text = parts.length === 1
      ? `${body}.`
      : `${index + 1}) ${body}${index === parts.length - 1 ? "." : ";"}`;
    return { ...part, text };
  });
}

export function buildReturnComment(selected, custom = "") {
  return returnCommentLines(selected, custom).map((line) => line.text).join("\n");
}

const newDraft = () => reactive({ selected: [], custom: "", touched: false, seededKey: "", reviewed: false });
const drafts = reactive({});

/** Черновик заказа; создаётся при первом обращении без лишних реактивных триггеров. */
export function returnDraft(id) {
  if (!id) return newDraft();
  const raw = toRaw(drafts);
  if (!raw[id]) raw[id] = newDraft();
  return raw[id];
}

export const peekReturnDraft = (id) => (id ? toRaw(drafts)[id] : undefined);

export function clearReturnDraft(id) {
  if (id) delete drafts[id];
}

export function clearReturnDrafts() {
  Object.keys(toRaw(drafts)).forEach((id) => delete drafts[id]);
}

/**
 * Комментарий по умолчанию для заказа, карточка которого не смонтирована
 * (другая страница): предложения проверки, если оператор не менял выбор.
 */
export function defaultReturnComment(order, id) {
  const draft = peekReturnDraft(id);
  if (draft?.touched) return "";
  return buildReturnComment(preselectedReasons(order));
}
