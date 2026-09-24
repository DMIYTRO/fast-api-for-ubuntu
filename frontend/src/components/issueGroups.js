export function groupIssueMessages(messages) {
  const groups = new Map();
  for (const message of messages) {
    const count = groups.get(message) || 0;
    groups.set(message, count + 1);
  }
  return [...groups].map(([message, count]) => ({ message, count }));
}

export function issueObjectLabel(count) {
  const remainder10 = count % 10;
  const remainder100 = count % 100;
  if (remainder10 === 1 && remainder100 !== 11) return "объект";
  if (remainder10 >= 2 && remainder10 <= 4 && (remainder100 < 12 || remainder100 > 14)) return "объекта";
  return "объектов";
}
