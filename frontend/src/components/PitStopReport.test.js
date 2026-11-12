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
    expect(items[1].textContent).toContain("1 объект, страницы: 2");
  });
});
