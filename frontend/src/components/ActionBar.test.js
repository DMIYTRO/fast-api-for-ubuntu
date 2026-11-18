import { afterEach, describe, expect, it } from "vitest";
import { createApp, nextTick } from "vue";
import ActionBar from "./ActionBar.vue";

const cleanup = [];
afterEach(() => cleanup.splice(0).forEach((fn) => fn()));
function mount(canPrint, needsPrintConfirmation = false) {
  const host = document.createElement("div");
  document.body.append(host);
  const actions = [];
  const app = createApp(ActionBar, {
    count: 1, selectedIds: ["100"], canPrint, needsPrintConfirmation, busy: false,
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

  it("asks for explicit confirmation before sending orders with errors", async () => {
    const actions = mount(true, true);
    await nextTick();
    const print = () => [...document.querySelectorAll(".action-bar button")]
      .find((button) => button.textContent === "Провести в печать");
    print().click();
    await nextTick();
    expect(actions).toEqual([]);
    expect(document.body.textContent).toContain("Заказ с ошибками. Отправить в печать под вашу ответственность?");
    [...document.querySelectorAll(".confirm-card button")].find((button) => button.textContent === "Отмена").click();
    await nextTick();
    expect(actions).toEqual([]);
    print().click();
    await nextTick();
    [...document.querySelectorAll(".confirm-card button")]
      .find((button) => button.textContent === "Отправить под мою ответственность").click();
    expect(actions).toEqual([["print", "", true]]);
  });
});

describe("ActionBar kit warning", () => {
  it("warns that Sborka returns the whole kit", async () => {
    const host = document.createElement("div");
    document.body.append(host);
    const app = createApp(ActionBar, {
      count: 2, selectedIds: ["a", "b"], canPrint: false, busy: false,
      kits: [{ main_id: "26234881", size: 19, selected: 2 }],
    });
    app.mount(host);
    cleanup.push(() => { app.unmount(); host.remove(); });
    await nextTick();
    [...document.querySelectorAll(".action-bar button")]
      .find((button) => button.textContent === "Вернуть на доработку").click();
    await nextTick();
    const warning = document.querySelector(".kit-return-warning");
    expect(warning?.textContent).toContain("Комплект 26234881 (19 заказов, выбрано 2)");
    expect(warning?.textContent).toContain("Sborka вернёт клиенту весь комплект");
  });
});
