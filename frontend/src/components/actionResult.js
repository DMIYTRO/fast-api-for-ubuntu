// Итог последнего действия «В печать» / «Вернуть» в карточке заказа: понятный текст и тон
// (зелёный — выполнено, жёлтый — нужно действие оператора, красный — не выполнено).
// Сырые статусы API (failed, rejected…) оператору не показываются.

export const LAYERED_REVIEW_TEXT = "Созданы PDF из слоёв TIFF — проверьте их ниже и нажмите «В печать» ещё раз.";
export const LAYERED_REVIEW_TOAST = "Проверьте созданные PDF из слоёв TIFF и отправьте в печать ещё раз";
const UNKNOWN_FAILURE = "Не удалось выполнить действие; причина не сохранена — повторите или посмотрите журнал.";

const TONES = {
  prepared: "ok",
  pending: "info",
  review_required: "attention",
  conflict: "attention",
  rejected: "error",
  error: "error",
  failed: "error",
  not_found: "error",
};

export function actionResultView(result) {
  if (!result) return null;
  const status = String(result.status || "");
  const message = typeof result.message === "string" ? result.message.trim() : "";
  const tone = TONES[status] || "info";
  let text;
  if (status === "prepared") text = "Подготовлено";
  else if (status === "pending") text = message || "Выполняется…";
  else if (status === "review_required") text = LAYERED_REVIEW_TEXT;
  else if (status === "conflict") text = message || "Конфликт файлов: выберите, заменить файл или сохранить под новым именем.";
  else if (status === "not_found") text = message || "Заказ не найден в сохранённой проверке.";
  else text = message || (tone === "error" ? UNKNOWN_FAILURE : "Действие выполнено.");
  return { status, tone, text };
}

// Неудачи, о которых говорит уведомление «Не удалось обработать…»; review_required — не сбой.
export const isActionFailure = (item) => !["prepared", "pending", "conflict", "review_required"].includes(item?.status);

// Уведомление после «В печать» / «Вернуть» по ответу API (список итогов по заказам).
export function actionToast(items, action) {
  const failed = items.filter(isActionFailure);
  if (failed.length) return `Не удалось обработать ${failed.length} заказ(а): ${actionResultView(failed[0]).text}`;
  if (items.some((item) => item.status === "review_required")) return LAYERED_REVIEW_TOAST;
  if (items.some((item) => item.status === "conflict")) return "Нужно решить конфликт файлов перед отправкой.";
  if (items.some((item) => item.status === "pending")) return "Загрузка превью запущена в фоне.";
  return action === "print" ? "Задание на печать подготовлено" : "Возврат подготовлен";
}
