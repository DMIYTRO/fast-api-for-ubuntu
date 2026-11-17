// Сравнение PitStop-проверки исправленного PDF с проверкой исходного PDF клиента.
// Бэкенд помечает находки, добавленные самим исправлением Callas
// (hidden_by_baseline / introduced_by_fix / baseline_occurrences): оператору они
// не показываются. Все потребители pitstop.issues и issue_groups берут данные
// отсюда, чтобы скрытое не просачивалось ни в списки, ни в группы, ни в уведомления.
// Для DTO без этих полей все функции возвращают данные без изменений.

const TRAILING_COUNT_RE = /\((\d+)(\s*[x×])(?=[\s)])([^()]*)\)([.\s]*)$/i;

export const isHiddenIssue = (issue) => Boolean(issue && typeof issue === "object" && issue.hidden_by_baseline === true);

const hasBaselineCount = (issue) => (
  issue && typeof issue === "object"
  && Number(issue.baseline_occurrences) > 0 && Number(issue.introduced_by_fix) > 0
);

// "Clipping objects found (1670x on pages 1-2)" -> "... (168x on pages 1-2)".
export function replaceTrailingCount(message, count) {
  if (typeof message !== "string") return message;
  return message.replace(TRAILING_COUNT_RE, (_, _n, mark, rest, tail) => `(${count}${mark}${rest})${tail}`);
}

// Находка в виде, пригодном для показа: частично добавленная исправлением показывает
// только количество клиента (в сообщении и в occurrences).
export function displayIssue(issue) {
  if (!hasBaselineCount(issue)) return issue;
  const count = Number(issue.baseline_occurrences);
  const adjusted = { ...issue, occurrences: count };
  if (typeof issue.message === "string") adjusted.message = replaceTrailingCount(issue.message, count);
  return adjusted;
}

// Находки PitStop для показа: без скрытых, с количеством клиента у частично добавленных.
export function visiblePitStopIssues(issues) {
  if (!Array.isArray(issues)) return [];
  return issues.filter((issue) => !isHiddenIssue(issue)).map(displayIssue);
}

const issueGroupKey = (issue) => {
  const actionId = issue.action_id;
  if (actionId != null && actionId !== "") return `action:${actionId}`;
  return String(issue.message || "").replace(/\d+(?:[.,]\d+)?/g, "#").replace(/\s+/g, " ").trim();
};

// Готовые группы бэкенда (order.issue_groups) без групп PitStop, состоящих только из
// скрытых находок, и с количеством клиента в текстах частично добавленных.
// Скрытая находка получает уровень "information", поэтому её группа отдельная (info).
export function visibleIssueGroups(groups, pitstop) {
  if (!Array.isArray(groups)) return groups;
  const issues = Array.isArray(pitstop?.issues) ? pitstop.issues : [];
  if (!issues.some((issue) => isHiddenIssue(issue) || hasBaselineCount(issue))) return groups;
  const hiddenKeys = new Set(issues.filter(isHiddenIssue).map(issueGroupKey));
  const shownInfoKeys = new Set(issues.filter((issue) => !isHiddenIssue(issue) && String(issue.severity || "").toLowerCase() === "information").map(issueGroupKey));
  const partial = issues.filter(hasBaselineCount).filter((issue) => typeof issue.message === "string");
  const fixText = (text) => {
    if (typeof text !== "string") return text;
    return partial.reduce((value, issue) => value.includes(issue.message) ? value.replace(issue.message, displayIssue(issue).message) : value, text);
  };
  return groups
    .filter((group) => !(group?.source === "pitstop" && group.severity === "info" && hiddenKeys.has(group.code) && !shownInfoKeys.has(group.code)))
    .map((group) => {
      if (group?.source !== "pitstop" || !partial.length) return group;
      return { ...group, message: fixText(group.message), samples: Array.isArray(group.samples) ? group.samples.map(fixText) : group.samples };
    });
}
