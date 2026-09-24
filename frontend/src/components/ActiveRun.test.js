import { afterEach, describe, expect, it } from "vitest";
import { createApp, nextTick } from "vue";
import ActiveRun from "./ActiveRun.vue";

const mounted = [];
function mount(run, fileProgress) {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(ActiveRun, { run, events: [], connection: "connected", fileProgress });
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return host;
}

afterEach(() => mounted.splice(0).forEach((unmount) => unmount()));

describe("ActiveRun progress state", () => {
  it("shows a confirmation state instead of spinning while files await operator action", async () => {
    const host = mount(
      { id: "run-1", status: "waiting_confirmation", stage: "Ожидание подтверждения", total_orders: 2 },
      {
        runId: "run-1",
        fileCountsByOrder: { completed: 90, pending: 3 },
        processedOrders: { completed: true },
        finalized: true,
      },
    );
    await nextTick();
    expect(host.querySelector(".run-throbber")).toBeNull();
    expect(host.querySelector(".run-waiting-marker")).not.toBeNull();
    expect(host.querySelector(".run-progress-label").textContent).toContain("Ожидает подтверждения");
    expect(host.querySelector(".run-progress-label").textContent).toContain("90 из 93 файлов");
    expect(host.querySelector(".run-progress-label").textContent).toContain("осталось 3");
  });
});
