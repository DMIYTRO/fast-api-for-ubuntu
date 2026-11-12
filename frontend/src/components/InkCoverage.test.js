import { afterEach, describe, expect, it } from "vitest";
import { createApp, nextTick } from "vue";
import InkCoverage from "./InkCoverage.vue";
import OrderCard from "./OrderCard.vue";
import { inkCoverageFlagTitle, inkCoverageMainFlag, inkCoverageTitle } from "./inkCoverage.js";

const mounted = [];
function mount(component, props) {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(component, props);
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return host;
}
afterEach(() => mounted.splice(0).forEach((unmount) => unmount()));

// Пример DTO: пороги 20 % / 250 %, страница 2 переливает.
const overflowData = {
  status: "completed",
  resolution: 72,
  box: "TrimBox",
  document: { min_percent: 48.5, max_percent: 262, min_page: 1, max_page: 2 },
  pages: [
    {
      page: 1, total_sum_percent: 48.5, process_sum_percent: 45.5,
      inks: [{ name: "Cyan", kind: "process", percent: 10 }, { name: "Black", kind: "process", percent: 35.5 }, { name: "PANTONE 185 C", kind: "spot", percent: 3 }],
    },
    {
      page: 2, total_sum_percent: 262, process_sum_percent: 262,
      inks: [{ name: "Cyan", kind: "process", percent: 70 }, { name: "Magenta", kind: "process", percent: 64 }, { name: "Yellow", kind: "process", percent: 60 }, { name: "Black", kind: "process", percent: 68 }],
    },
  ],
  flags: [{ kind: "overflow", page: 2, percent: 262, threshold: 250 }],
  thresholds: { min_percent: 20, max_percent: 250 },
  error: null,
};
const underflowData = {
  ...overflowData,
  document: { min_percent: 11.1, max_percent: 48.5, min_page: 1, max_page: 2 },
  flags: [{ kind: "underflow", page: 1, percent: 11.1, threshold: 20 }],
};
const cleanData = { ...overflowData, flags: [] };
const order = (ink_coverage) => ({
  order_id: "26128104", customer_id: "22557", status: "passed", pitstop_status: "not_checked",
  errors: [], warnings: [], processing_errors: [], files: [{ id: 1, filename: "26128104_4-4_(90x50).tif", parsed: { front_colors: 4, back_colors: 4 } }], ink_coverage,
});

describe("inkCoverage helpers", () => {
  it("builds the operator title with percent, page and threshold", () => {
    expect(inkCoverageFlagTitle({ kind: "overflow", page: 2, percent: 262, threshold: 250 })).toBe("Перелив: 262% на стр. 2 (порог 250%)");
    expect(inkCoverageFlagTitle({ kind: "underflow", page: 1, percent: 11.1, threshold: 20 })).toBe("Недолив: 11.1% на стр. 1 (порог 20%)");
    expect(inkCoverageTitle({ status: "completed", flags: [...underflowData.flags, ...overflowData.flags] })).toBe("Недолив: 11.1% на стр. 1 (порог 20%); Перелив: 262% на стр. 2 (порог 250%)");
  });

  it("prefers overflow as the main flag and ignores missing data", () => {
    expect(inkCoverageMainFlag({ status: "completed", flags: [...underflowData.flags, ...overflowData.flags] }).kind).toBe("overflow");
    expect(inkCoverageMainFlag(undefined)).toBeNull();
    expect(inkCoverageMainFlag({ ...overflowData, status: "failed" })).toBeNull();
    expect(inkCoverageMainFlag({ status: "completed", flags: [] })).toBeNull();
  });
});

describe("InkCoverage details", () => {
  it("lists per-page sums and marks the flagged page", async () => {
    const host = mount(InkCoverage, { data: overflowData });
    await nextTick();
    const rows = [...host.querySelectorAll("tbody tr")];
    expect(rows).toHaveLength(2);
    expect(rows[0].textContent).toContain("48.5%");
    expect(rows[0].textContent).toContain("PANTONE 185 C 3%");
    expect(rows[1].classList.contains("overflow")).toBe(true);
    expect(rows[1].textContent).toContain("262%");
    expect(rows[1].textContent).toContain("Перелив");
    expect(host.textContent).toContain("порог 20%–250%");
    expect(host.textContent).toContain("не TAC");
  });

  it("explains a failed check instead of showing a table", async () => {
    const host = mount(InkCoverage, { data: { status: "failed", error: "Callas недоступен", pages: [], flags: [] } });
    await nextTick();
    expect(host.querySelector("table")).toBeNull();
    expect(host.textContent).toContain("Красочность не рассчитана: Callas недоступен");
  });
});

describe("OrderCard ink coverage pictogram", () => {
  it("shows Перелив next to Красочность with the actual percent and page", async () => {
    const host = mount(OrderCard, { order: order(overflowData), runId: "run-1", selected: false, reasons: [] });
    await nextTick();
    const badge = host.querySelector(".order-meta .ink-indicator");
    expect(badge).not.toBeNull();
    expect(badge.classList.contains("overflow")).toBe(true);
    expect(badge.textContent).toContain("Перелив");
    expect(badge.getAttribute("title")).toBe("Перелив: 262% на стр. 2 (порог 250%)");
    expect(badge.getAttribute("aria-label")).toBe("Перелив: 262% на стр. 2 (порог 250%)");
  });

  it("shows Недолив for an underflow flag", async () => {
    const host = mount(OrderCard, { order: order(underflowData), runId: "run-1", selected: false, reasons: [] });
    await nextTick();
    const badge = host.querySelector(".order-meta .ink-indicator");
    expect(badge.classList.contains("underflow")).toBe(true);
    expect(badge.textContent).toContain("Недолив");
    expect(badge.getAttribute("title")).toBe("Недолив: 11.1% на стр. 1 (порог 20%)");
  });

  it("hides the pictogram without flags or data but still offers details", async () => {
    const clean = mount(OrderCard, { order: order(cleanData), runId: "run-1", selected: false, reasons: [] });
    const none = mount(OrderCard, { order: order(undefined), runId: "run-1", selected: false, reasons: [] });
    await nextTick();
    expect(clean.querySelector(".ink-indicator")).toBeNull();
    expect(none.querySelector(".ink-indicator")).toBeNull();
    expect(clean.querySelector(".order-meta").textContent).toContain("Красочность 4-4");

    clean.querySelector(".details-toggle").click();
    none.querySelector(".details-toggle").click();
    await nextTick();
    expect(clean.querySelector(".ink-coverage")).not.toBeNull();
    expect(clean.querySelector(".ink-coverage").textContent).toContain("48.5%");
    expect(none.querySelector(".ink-coverage")).toBeNull();
  });
});
