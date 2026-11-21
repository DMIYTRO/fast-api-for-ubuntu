import { afterEach, describe, expect, it } from "vitest";
import { createApp, nextTick } from "vue";
import OrderJournal from "./OrderJournal.vue";
import { entryMeta, groupJournal, isMuted } from "./orderJournal.js";

const journal = {
  schema: "image-magic.order-journal",
  version: 1,
  entries: [
    { kind: "issue", code: "pitstop:2079", source: "pitstop", severity: "info", title: "Обтравка: Clipping objects found", pages: [1, 2], count: 3, at: null, outcome: null, data: { policy_demoted: true } },
    { kind: "issue", code: "pitstop:1153", source: "pitstop", severity: "info", title: "Суммарное покрытие", pages: [1], count: 4, at: null, outcome: null, data: { added_by_fix: true } },
    { kind: "issue", code: "prepress:black_solid", source: "callas", severity: "warning", title: "Плашка 100% K: 41% площади", pages: [1], count: null, at: null, outcome: null, data: {} },
    { kind: "issue", code: "prepress:high_ppi", source: "callas", severity: "warning", title: "Растры выше 500 ppi", pages: [2], count: 2, at: null, outcome: "resolved", data: { resolved: true } },
    { kind: "auto_change", code: "auto:size_fix:bleed", source: "callas", severity: null, title: "Добавлены поля вылета (Callas)", file: "face.pdf", pages: null, count: null, at: "2026-11-21T10:00:00+00:00", outcome: "applied", data: {} },
    { kind: "decision", code: "action:print", source: "operator", severity: null, title: "Передано в печать", at: "2026-11-21T10:05:00+00:00", outcome: "done", data: {} },
    { kind: "operator_change", code: "callas_fix:reduce_tac", source: "callas", severity: null, title: "Исправление Callas: Снижение суммарного покрытия до 300%", detail: "Принято оператором; новая ревизия PDF r2", at: "2026-11-21T10:02:30+00:00", outcome: "accepted", data: {} },
    null,
  ],
};

let app; let host;
afterEach(() => { app?.unmount(); host?.remove(); app = null; });

describe("groupJournal", () => {
  it("groups entries into the four sections in a fixed order and sets aside fix-added findings", () => {
    const sections = groupJournal(journal);
    expect(sections.map((section) => section.title)).toEqual(["Найдено в файлах", "Исправлено автоматически", "Действия оператора", "Решение"]);
    expect(sections[0].entries.map((entry) => entry.code)).toEqual(["pitstop:2079", "prepress:black_solid", "prepress:high_ppi"]);
    expect(sections[0].hidden.map((entry) => entry.code)).toEqual(["pitstop:1153"]);
    expect(groupJournal({ entries: [] })).toEqual([]);
    expect(groupJournal(null)).toEqual([]);
  });

  it("mutes demoted, fix-added and resolved findings and formats meta", () => {
    expect(isMuted(journal.entries[0])).toBe(true);
    expect(isMuted(journal.entries[3])).toBe(true);
    expect(isMuted(journal.entries[2])).toBe(false);
    expect(entryMeta(journal.entries[0])).toBe("стр. 1, 2 · ×3");
    expect(entryMeta(journal.entries[4], () => "21 нояб.")).toBe("face.pdf · 21 нояб.");
  });
});

describe("OrderJournal", () => {
  it("renders sections, severity chips, outcome badges and notes", async () => {
    host = document.createElement("div"); document.body.append(host);
    app = createApp(OrderJournal, { journal }); app.mount(host);
    await nextTick();
    const text = host.textContent;
    expect([...host.querySelectorAll("h3")].map((node) => node.textContent)).toEqual(["Найдено в файлах", "Исправлено автоматически", "Действия оператора", "Решение"]);
    expect(host.querySelector(".journal-chip.warning").textContent).toBe("Предупреждение");
    expect(text).toContain("Не влияет на статус (облегчённая проверка)");
    expect(text).toContain("Устранено");
    expect(text).toContain("Принято");
    expect(text).toContain("Добавлено исправлением Callas (1)");
    expect(host.querySelectorAll(".journal-entry.muted").length).toBe(3);
  });

  it("shows an empty state", () => {
    host = document.createElement("div"); document.body.append(host);
    app = createApp(OrderJournal, { journal: { entries: [] } }); app.mount(host);
    expect(host.textContent).toContain("Записей в журнале нет.");
  });
});
