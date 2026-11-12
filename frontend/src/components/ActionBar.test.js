import { afterEach, describe, expect, it } from "vitest";
import { createApp, nextTick } from "vue";
import ActionBar from "./ActionBar.vue";

const cleanup = [];
afterEach(() => cleanup.splice(0).forEach((fn) => fn()));
function mount(canPrint) {
  const host = document.createElement("div");
  document.body.append(host);
  const actions = [];
  const app = createApp(ActionBar, {
    count: 1, selectedIds: ["100"], canPrint, busy: false,
    onAction: (...args) => actions.push(args),
  });
  app.mount(host);
  cleanup.push(() => { app.unmount(); host.remove(); });
  return actions;
}

describe("ActionBar print eligibility", () => {
  it("offers no override when the selected orders failed mandatory checks", async () => {
    mount(false);
    await nextTick();
    const buttons = [...document.querySelectorAll(".action-bar button")];
    expect(buttons.map((button) => button.textContent)).not.toContain("Отправить в работу");
    const print = buttons.find((button) => button.textContent === "Провести в печать");
    expect(print.disabled).toBe(true);
  });

  it("submits the ordinary print action for eligible orders", async () => {
    const actions = mount(true);
    await nextTick();
    [...document.querySelectorAll(".action-bar button")]
      .find((button) => button.textContent === "Провести в печать").click();
    expect(actions).toEqual([["print", ""]]);
  });
});
