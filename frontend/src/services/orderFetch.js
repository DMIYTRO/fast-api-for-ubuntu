// «Получить заказ»: перенос файлов заказов из сетевых папок во входную папку (services/order_fetch.py).
import { request } from "./api.js";

export const orderFetchApi = {
  start: () => request("/api/order-fetch", { method: "POST" }),
  status: () => request("/api/order-fetch"),
};

export const FETCH_RUNNING_STATES = ["scanning", "copying"];
export const isFetchRunning = (snapshot) => FETCH_RUNNING_STATES.includes(snapshot?.state);

const MB = 1024 * 1024;
const count = (value) => {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
};
// 0,4 МБ · 12,5 МБ · 345 МБ — одна дробная цифра до сотни, дальше целые.
export function formatMb(bytes) {
  const value = count(bytes) / MB;
  return (value >= 100 ? value.toFixed(0) : value.toFixed(1)).replace(".", ",");
}

export function fetchProgressLabel(snapshot) {
  if (!snapshot || snapshot.state === "scanning") return "Поиск файлов в сетевых папках…";
  const done = count(snapshot.done_files);
  const total = count(snapshot.total_files);
  const speed = formatMb(snapshot.bytes_per_second);
  return `Копирование: ${done} из ${total} файлов · ${formatMb(snapshot.done_bytes)} из ${formatMb(snapshot.total_bytes)} МБ · ${speed} МБ/с`;
}

const SKIP_LABELS = {
  zero_size: "пустые",
  still_writing: "ещё записываются",
  subfolder: "подпапки",
  already_present: "уже в рабочей папке",
  source_vanished: "исчезли из источника",
};
// Неизвестный код пропуска группируется по reason, поэтому новый код бэкенда не теряется.

// 0,4 с · 12 с · 2 мин 5 с
export function formatDuration(seconds) {
  const value = Number(seconds);
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 10) return `${value.toFixed(1).replace(".", ",")} с`;
  const total = Math.round(value);
  if (total < 60) return `${total} с`;
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return rest ? `${minutes} мин ${rest} с` : `${minutes} мин`;
}
const items = (value) => (Array.isArray(value) ? value : []);
const line = (item) => `${item?.name || item?.source || "файл"} — ${item?.reason || item?.code || "причина не указана"}`;

// Сводка завершённой задачи для оператора: счётчики, пропуски по причинам, ошибки списком.
export function summarizeFetch(snapshot) {
  const result = snapshot?.result || {};
  const moved = items(result.moved);
  const copied = items(result.copied);
  const skipped = items(result.skipped);
  const failed = items(result.failed);
  // severity "warning" (например, папки-источника нет на сервере) — неброское предупреждение,
  // "error" (не смонтировано, нет доступа) — сбой; без severity считаем ошибкой.
  // Одна и та же причина у нескольких папок (например, «не смонтировано» у всех четырёх
  // источников одной шары) показывается одной строкой.
  const seenIssues = new Set();
  const sourceIssues = items(result.source_errors)
    .map((error) => ({
      text: error?.message || (error?.source ? `Ошибка папки ${error.source}` : "Ошибка папки-источника"),
      source: error?.source || "",
      code: error?.code || "",
      severity: error?.severity === "warning" ? "warning" : "error",
    }))
    .filter((issue) => {
      const key = `${issue.severity}\u0000${issue.text}`;
      if (seenIssues.has(key)) return false;
      seenIssues.add(key);
      return true;
    });
  const sourceErrors = sourceIssues.filter((issue) => issue.severity === "error");
  const sourceWarnings = sourceIssues.filter((issue) => issue.severity === "warning");

  const skipCounts = new Map();
  for (const item of skipped) {
    const label = SKIP_LABELS[item?.code] || item?.reason || "прочее";
    skipCounts.set(label, (skipCounts.get(label) || 0) + 1);
  }
  const skipBreakdown = [...skipCounts].map(([label, number]) => `${label} ${number}`).join(", ");

  // Скопирован, но исходник не удалён (delete_failed) — копия на месте, причину стоит показать.
  const copiedWithIssue = copied.filter((item) => item?.reason);
  const parts = [`Перенесено ${moved.length}`];
  if (copied.length) parts.push(`скопировано без удаления исходника ${copied.length}`);
  parts.push(skipped.length ? `пропущено ${skipped.length} (${skipBreakdown})` : "пропущено 0");
  parts.push(`ошибки ${failed.length}`);
  // Время и средняя скорость — только если что-то действительно передано.
  const duration = formatDuration(result.duration_seconds);
  if ((moved.length || copied.length) && duration) {
    parts.push(`за ${duration}`);
    if (result.bytes_per_second != null) parts.push(`${formatMb(result.bytes_per_second)} МБ/с`);
  }

  const failedState = snapshot?.state === "failed";
  const cancelled = snapshot?.state === "cancelled";
  const empty = !moved.length && !copied.length && !skipped.length && !failed.length;
  let headline = parts.join(" · ");
  if (failedState) {
    const reason = snapshot?.error || snapshot?.message || result.error || "";
    headline = `Получение заказов завершилось с ошибкой${reason ? `: ${reason}` : ""}`;
  } else if (cancelled) {
    headline = `Получение прервано · ${headline}`;
  } else if (empty && !sourceErrors.length) {
    headline = "Новых файлов нет";
  }

  return {
    headline,
    ok: !failedState && !cancelled && !failed.length && !sourceErrors.length && !copiedWithIssue.length,
    moved: moved.length,
    copied: copied.length,
    skipped: skipped.length,
    failed: failed.length,
    problems: [...failed.map(line), ...copiedWithIssue.map(line)],
    sourceErrors,
    sourceWarnings,
  };
}
