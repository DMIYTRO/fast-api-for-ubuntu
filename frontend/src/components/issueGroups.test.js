import { describe, expect, it } from "vitest";
import { groupIssueMessages, issueObjectLabel } from "./issueGroups.js";

describe("groupIssueMessages", () => {
  it("groups exact duplicate messages and keeps distinct errors separate", () => {
    expect(groupIssueMessages(["Низкий DPI", "Низкий DPI", "Нет шрифтов"])).toEqual([
      { message: "Низкий DPI", count: 2 },
      { message: "Нет шрифтов", count: 1 },
    ]);
  });

  it("groups repeated warning messages without mixing distinct warnings", () => {
    expect(groupIssueMessages(["Цветовой профиль не указан", "Цветовой профиль не указан", "Проверьте размер"])).toEqual([
      { message: "Цветовой профиль не указан", count: 2 },
      { message: "Проверьте размер", count: 1 },
    ]);
  });

  it("uses correct Russian plural forms for the object count", () => {
    expect([1, 2, 5, 11, 21, 24, 25].map(issueObjectLabel)).toEqual([
      "объект", "объекта", "объектов", "объектов", "объект", "объекта", "объектов",
    ]);
  });
});
