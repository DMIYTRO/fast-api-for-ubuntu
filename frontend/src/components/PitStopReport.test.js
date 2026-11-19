import { afterEach, describe, expect, it } from "vitest";
import { createApp, nextTick } from "vue";
import PitStopReport from "./PitStopReport.vue";

const mounted = [];
function mount(pitstop) {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(PitStopReport, { pitstop });
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return host;
}
afterEach(() => mounted.splice(0).forEach((unmount) => unmount()));

describe("PitStopReport issue grouping", () => {
  it("shows one line per issue type with object and page summary", async () => {
    const issues = Array.from({ length: 12 }, (_, i) => ({
      severity: "error", action_id: 2078, message: "RGB color is used",
      locations: [{ page: 1 + (i % 3), bbox: { min_x: i, min_y: 0, max_x: i + 1, max_y: 1 } }],
    }));
    issues.push({ severity: "warning", action_id: 1001, message: "Font not embedded", locations: [{ page: 2, bbox: {} }] });
    const host = mount({ execution_status: "completed", verdict: "error", counts: { errors: 12, warnings: 1 }, issues });
    await nextTick();
    expect(host.querySelector(".pitstop-toggle").textContent).toContain("(2)");
    host.querySelector(".pitstop-toggle").click();
    await nextTick();
    const items = [...host.querySelectorAll(".pitstop-issues li")];
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain("RGB color is used");
    expect(items[0].textContent).toContain("12 объектов, страницы: 1, 2, 3");
    expect(items[1].textContent).toContain("Font not embedded");
    expect(items[1].textContent).toContain("страницы: 2");
    expect(items[1].textContent).not.toContain("1 объект");
  });
});

describe("PitStopReport fix baseline", () => {
  const hiddenIssue = {
    severity: "information", original_severity: "warning", hidden_by_baseline: true, introduced_by_fix: 1670, baseline_occurrences: 0,
    action_id: 2079, occurrences: 1670, message: "Clipping objects found (1670x on pages 1-2)",
    locations: [{ page: 1, bbox: { min_x: 0, min_y: 0, max_x: 1, max_y: 1 } }],
  };
  const partialIssue = {
    severity: "warning", baseline_occurrences: 168, introduced_by_fix: 1502,
    action_id: 3004, occurrences: 1670, message: "Transparency found (1670x on pages 1-2)",
    locations: [{ page: 1, bbox: { min_x: 0, min_y: 0, max_x: 1, max_y: 1 } }],
  };
  const font = { severity: "error", action_id: 1001, message: "Font not embedded", locations: [{ page: 2, bbox: {} }] };

  it("does not render or count hidden issues and uses the adjusted counts", async () => {
    const host = mount({
      execution_status: "completed", verdict: "passed", verdict_raw: "warning", baseline_status: "applied",
      counts: { errors: 0, warnings: 0 }, counts_raw: { errors: 0, warnings: 1 }, issues: [hiddenIssue],
    });
    await nextTick();
    expect(host.querySelector(".pitstop-toggle")).toBeNull();
    expect(host.querySelectorAll(".pitstop-issues li")).toHaveLength(0);
    expect(host.textContent).not.toContain("Clipping objects");
    expect(host.textContent).not.toContain("1670");
    expect(host.textContent).not.toContain("applied");
    const meta = host.querySelector(".pitstop-meta").textContent;
    expect(meta).toMatch(/Ошибок\s*0/);
    expect(meta).toMatch(/Предупреждений\s*0/);
    expect(host.querySelector(".pitstop-report").classList.contains("pitstop-passed")).toBe(true);
  });

  it("keeps visible issues next to a hidden one", async () => {
    const host = mount({ execution_status: "completed", verdict: "error", counts: { errors: 1, warnings: 0 }, issues: [hiddenIssue, font] });
    await nextTick();
    expect(host.querySelector(".pitstop-toggle").textContent).toContain("(1)");
    host.querySelector(".pitstop-toggle").click();
    await nextTick();
    const items = [...host.querySelectorAll(".pitstop-issues li")];
    expect(items).toHaveLength(1);
    expect(items[0].textContent).toContain("Font not embedded");
  });

  it("shows only the client's count for a partially introduced issue", async () => {
    const host = mount({ execution_status: "completed", verdict: "warning", counts: { errors: 0, warnings: 1 }, issues: [partialIssue] });
    await nextTick();
    host.querySelector(".pitstop-toggle").click();
    await nextTick();
    const text = host.querySelector(".pitstop-issues").textContent;
    expect(text).toContain("Transparency found (168x on pages 1-2)");
    expect(text).not.toContain("1670");
    expect(text).not.toContain("1502");
  });

  it("renders a DTO without baseline fields exactly as before", async () => {
    const issue = { severity: "warning", action_id: 2079, occurrences: 1670, message: "Clipping objects found (1670x on pages 1-2)", locations: [{ page: 1, bbox: {} }] };
    const host = mount({ execution_status: "completed", verdict: "warning", counts: { errors: 0, warnings: 1 }, issues: [issue] });
    await nextTick();
    host.querySelector(".pitstop-toggle").click();
    await nextTick();
    const items = [...host.querySelectorAll(".pitstop-issues li")];
    expect(items).toHaveLength(1);
    expect(items[0].textContent).toContain("Clipping objects found (1670x on pages 1-2)");
    expect(items[0].textContent).toContain("страницы: 1");
  });
});

describe("PitStopReport check policy", () => {
  const demoted = { severity: "information", policy_demoted: true, policy_original_severity: "warning", policy_category: "transparency", policy_category_label: "Прозрачность", action_id: 2080, message: "Transparency used", locations: [] };
  const unclassified = { severity: "warning", policy_unclassified: true, action_id: 9999, message: "Brand new check", locations: [] };

  it("shows a demoted issue as information with a note and the category", async () => {
    const host = mount({ execution_status: "completed", verdict: "passed", check_policy: "basic", counts: { errors: 0, warnings: 0 }, issues: [demoted] });
    await nextTick();
    host.querySelector(".pitstop-toggle").click();
    await nextTick();
    const item = host.querySelector(".pitstop-issues li");
    expect(item.classList.contains("issue-info")).toBe(true);
    expect(item.textContent).toContain("не влияет на статус (облегчённая проверка)");
    expect(item.textContent).toContain("Прозрачность");
    expect(host.querySelector(".pitstop-report").classList.contains("pitstop-passed")).toBe(true);
    expect(host.querySelector(".pitstop-policy-note")?.textContent).toContain("Облегчённая проверка (Input)");
  });

  it("hints that an unclassified issue needs a category", async () => {
    const host = mount({ execution_status: "completed", verdict: "warning", check_policy: "basic", counts: { warnings: 1 }, issues: [unclassified] });
    await nextTick();
    host.querySelector(".pitstop-toggle").click();
    await nextTick();
    const item = host.querySelector(".pitstop-issues li");
    expect(item.textContent).toContain("новая проверка: не классифицирована");
    expect(item.textContent).not.toContain("не влияет на статус");
  });

  it("shows no policy notes for full checks and old reports", async () => {
    const host = mount({ execution_status: "completed", verdict: "error", counts: { errors: 1 }, issues: [{ severity: "error", action_id: 1, message: "Bad", locations: [] }] });
    await nextTick();
    host.querySelector(".pitstop-toggle").click();
    await nextTick();
    expect(host.querySelector(".policy-note")).toBeNull();
    expect(host.querySelector(".pitstop-policy-note")).toBeNull();
  });
});
