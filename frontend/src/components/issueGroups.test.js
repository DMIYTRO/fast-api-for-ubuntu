import { describe, expect, it } from "vitest";
import {
  buildIssueGroups,
  formatGroupMessage,
  groupIssueMessages,
  groupPitStopIssues,
  issueObjectLabel,
  normalizeIssueMessage,
  orderIssueGroups,
} from "./issueGroups.js";

const pitstopIssue = (message, page, bbox = [0, 0, 10, 10], extra = {}) => ({
  severity: "error",
  action_id: 2078,
  message,
  locations: [{ page, bbox: { min_x: bbox[0], min_y: bbox[1], max_x: bbox[2], max_y: bbox[3] }, units: "pt" }],
  ...extra,
});

describe("groupIssueMessages", () => {
  it("groups exact duplicate messages and keeps distinct errors separate", () => {
    expect(groupIssueMessages(["Низкий DPI", "Низкий DPI", "Нет шрифтов"])).toEqual([
      { message: "Низкий DPI", count: 2 },
      { message: "Нет шрифтов", count: 1 },
    ]);
  });

  it("collapses messages that differ only by numbers", () => {
    expect(groupIssueMessages([
      "размер 90.2x50.1 мм; ожидается 94.0x54.0 мм",
      "размер 88.0x48.0 мм; ожидается 94.0x54.0 мм",
      "Неизвестное 17 сообщение",
      "Неизвестное 18 сообщение",
    ])).toEqual([
      { message: "Размер не соответствует ожидаемому", count: 2 },
      { message: "Неизвестное 17 сообщение", count: 2 },
    ]);
  });

  it("uses correct Russian plural forms for the object count", () => {
    expect([1, 2, 5, 11, 21, 24, 25].map(issueObjectLabel)).toEqual([
      "объект", "объекта", "объектов", "объектов", "объект", "объекта", "объектов",
    ]);
  });
});

describe("normalizeIssueMessage", () => {
  it("maps known templates to codes and collapses digits otherwise", () => {
    expect(normalizeIssueMessage("PDF страница 2: effective DPI 120.0; требуется не менее 300").code).toBe("pdf_low_dpi");
    expect(normalizeIssueMessage("TIFF содержит альфа-канал; перед отправкой удалите прозрачность").code).toBe("tiff_alpha");
    expect(normalizeIssueMessage("Что-то 12 раз по 3.5 мм")).toEqual({ code: "Что-то # раз по # мм", label: "Что-то 12 раз по 3.5 мм" });
  });
});

describe("groupPitStopIssues", () => {
  it("turns 100 identical entries into one group", () => {
    const groups = groupPitStopIssues(Array.from({ length: 100 }, () => pitstopIssue("Low resolution image", 1)));
    expect(groups).toHaveLength(1);
    expect(groups[0]).toMatchObject({ count: 100, objects: 1, pages: [1], code: "action:2078", severity: "error", source: "pitstop" });
  });

  it("counts unique objects and pages from locations", () => {
    const issues = Array.from({ length: 20 }, (_, i) => pitstopIssue("Image resolution too low", 1 + (i % 2), [i, i, i + 5, i + 5]));
    const [group] = groupPitStopIssues(issues);
    expect(group.objects).toBe(20);
    expect(group.pages).toEqual([1, 2]);
    expect(group.message).toBe("Image resolution too low — 20 объектов на страницах 1, 2");
  });

  it("keeps different codes and severities apart", () => {
    const groups = groupPitStopIssues([
      pitstopIssue("RGB color is used", 1),
      pitstopIssue("Font not embedded", 1, undefined, { action_id: 1001 }),
      pitstopIssue("RGB color is used", 1, undefined, { severity: "warning" }),
      { severity: "information", message: "Page 1 has 3 images" },
    ]);
    expect(groups.map((g) => [g.severity, g.code])).toEqual([
      ["error", "action:2078"], ["error", "action:1001"], ["warning", "action:2078"], ["info", "Page # has # images"],
    ]);
    expect(groups[3].objects).toBeNull();
  });
});

describe("buildIssueGroups / orderIssueGroups", () => {
  it("prefers backend issue_groups when present", () => {
    const backend = [{ code: "x", severity: "error", source: "local", message: "X", count: 1, objects: null, pages: [], files: [], samples: ["X"] }];
    expect(orderIssueGroups({ issue_groups: backend, errors: ["a", "b"] })).toBe(backend);
  });

  it("builds groups from raw order payloads including files and pitstop", () => {
    const groups = buildIssueGroups({
      errors: [],
      processing_errors: ["PDF: Ghostscript не смог создать файл"],
      warnings: ["стороны заказа имеют разные форматы: PDF, TIFF"],
      files: [
        { filename: "a_face.tif", errors: ["разрешение 150 DPI; по обеим осям требуется не меньше 300 DPI"] },
        { filename: "a_back.tif", errors: ["разрешение 200 DPI; по обеим осям требуется не меньше 300 DPI"] },
      ],
      pitstop: { issues: [pitstopIssue("RGB color is used", 1)] },
    });
    expect(groups.map((g) => [g.source, g.severity, g.code])).toEqual([
      ["local", "error", "pdf_error"],
      ["local", "error", "low_dpi"],
      ["pitstop", "error", "action:2078"],
      ["local", "warning", "mixed_formats"],
    ]);
    expect(groups[1]).toMatchObject({ count: 2, files: ["a_face.tif", "a_back.tif"], message: "Низкое разрешение — 2 повтора в 2 файлах" });
  });

  it("formats summaries with Russian plurals", () => {
    expect(formatGroupMessage("X", { count: 1 })).toBe("X");
    expect(formatGroupMessage("X", { count: 1, objects: 1, pages: [3] })).toBe("X — 1 объект на странице 3");
    expect(formatGroupMessage("X", { count: 3, files: ["a.tif"] })).toBe("X — 3 повтора в файле a.tif");
  });
});


it("does not infer objects from page-only or partial locations", () => {
  const [pageOnly] = groupPitStopIssues([{ severity: "error", message: "DPI", locations: [{ page: 2 }, { page: 2 }] }]);
  expect(pageOnly.objects).toBeNull();
  expect(pageOnly.pages).toEqual([2]);
  const [partial] = groupPitStopIssues([pitstopIssue("DPI", 1), { severity: "error", action_id: 2078, message: "DPI" }]);
  expect(partial.objects).toBeNull();
});
