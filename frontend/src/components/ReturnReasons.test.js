import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createApp, nextTick, reactive } from "vue";
import ReturnReasons from "./ReturnReasons.vue";
import { clearReturnDrafts, defaultReturnComment } from "../stores/returnDrafts.js";
import { orderIdentity } from "../stores/orderIdentity.js";

const reasons = [
  { category: "Разрешение", text: "Низкое разрешение изображений" },
  { category: "Размер", text: "Неверный размер макета" },
  { category: "Цвет", text: "Макет в RGB" },
  { category: "Прочее", text: "Нет вылетов" },
];
const suggestions = [
  { text: "Низкое разрешение изображений", category: "Разрешение", signal: "pitstop:1150", confidence: "high", preselect: true, evidence: "PitStop 1150: 197 ppi" },
  { text: "Неверный размер макета", category: "Размер", signal: "size", confidence: "medium", preselect: false, evidence: "90×50 вместо 94×54" },
  { text: "Макет в RGB", category: "Цвет", signal: "rgb", confidence: "low", preselect: false, evidence: "" },
];

const mounted = [];
function mount(order) {
  const host = document.createElement("div");
  document.body.append(host);
  const changes = [];
  const app = createApp(ReturnReasons, { order, reasons, onChange: (value) => changes.push(value) });
  app.mount(host);
  const unmount = () => { app.unmount(); host.remove(); };
  mounted.push(unmount);
  return { host, changes, unmount };
}
async function openDialog(host) {
  host.querySelector(".return-trigger").click();
  await nextTick();
  return document.querySelector(".return-dialog");
}
const suggestionBoxes = (dialog) => [...dialog.querySelectorAll(".return-suggestion input")];
const last = (items) => items[items.length - 1];

beforeEach(() => clearReturnDrafts());
afterEach(() => mounted.splice(0).forEach((unmount) => unmount()));

describe("ReturnReasons suggestions", () => {
  it("pre-checks only high-confidence suggestions and emits the comment on mount", async () => {
    const { host, changes } = mount({ order_id: "1", status: "error", return_suggestions: suggestions });
    await nextTick();
    expect(last(changes)).toBe("Низкое разрешение изображений.");
    expect(host.querySelector(".return-badge")?.textContent).toBe("1");

    const dialog = await openDialog(host);
    const boxes = suggestionBoxes(dialog);
    expect(boxes.map((box) => box.checked)).toEqual([true, false, false]);
    const block = dialog.querySelector(".return-suggestions").textContent;
    expect(block).toContain("Предложено проверкой");
    expect(block).toContain("PitStop 1150: 197 ppi");
    expect(block).toContain("точно");
    expect(block).toContain("возможно");
    expect(block).toContain("на усмотрение");
    // Предложенные причины не дублируются в общем списке, пока поиск пуст.
    const catalogue = [...dialog.querySelectorAll(".return-options .return-option")].map((row) => row.textContent);
    expect(catalogue).toHaveLength(1);
    expect(catalogue[0]).toContain("Нет вылетов");
  });

  it("toggles the same selection from the suggestion block", async () => {
    const { host, changes } = mount({ order_id: "2", status: "warning", return_suggestions: suggestions });
    const dialog = await openDialog(host);
    suggestionBoxes(dialog)[1].click();
    await nextTick();
    expect(last(changes)).toBe("1) Низкое разрешение изображений;\n2) Неверный размер макета.");
  });

  it("does not pre-check passed orders but still lists their suggestions", async () => {
    const { host, changes } = mount({ order_id: "3", status: "passed", return_suggestions: suggestions });
    await nextTick();
    expect(last(changes)).toBe("");
    expect(host.querySelector(".return-badge")).toBeNull();
    const dialog = await openDialog(host);
    expect(suggestionBoxes(dialog).every((box) => !box.checked)).toBe(true);
  });

  it("is safe without return_suggestions", async () => {
    const { host, changes } = mount({ order_id: "4", status: "error" });
    await nextTick();
    expect(last(changes)).toBe("");
    expect(host.querySelector(".return-trigger").title).toBe("Причины возврата");
    expect(host.querySelector(".return-trigger").classList.contains("has-suggestions")).toBe(false);
    const dialog = await openDialog(host);
    expect(dialog.querySelector(".return-suggestions")).toBeNull();
    expect(dialog.querySelectorAll(".return-options .return-option")).toHaveLength(4);
  });

  it("marks unreviewed suggestions on the trigger until the dialog is opened", async () => {
    const { host } = mount({ order_id: "5", status: "error", return_suggestions: suggestions });
    await nextTick();
    const trigger = host.querySelector(".return-trigger");
    expect(trigger.title).toBe("Причины возврата · Предложено проверкой: 3");
    expect(trigger.classList.contains("has-suggestions")).toBe(true);
    await openDialog(host);
    expect(trigger.classList.contains("has-suggestions")).toBe(false);
  });

  it("follows suggestion updates until the operator touches the selection", async () => {
    const order = reactive({ order_id: "6", status: "error", return_suggestions: [] });
    const { changes } = mount(order);
    await nextTick();
    expect(last(changes)).toBe("");
    order.return_suggestions = suggestions;
    await nextTick();
    expect(last(changes)).toBe("Низкое разрешение изображений.");
    order.return_suggestions = [{ ...suggestions[1], preselect: true, confidence: "high" }];
    await nextTick();
    expect(last(changes)).toBe("Неверный размер макета.");
  });

  it("never re-adds an unchecked suggestion on update or remount", async () => {
    const order = reactive({ order_id: "7", customer_id: "9", status: "error", return_suggestions: suggestions });
    const first = mount(order);
    const dialog = await openDialog(first.host);
    suggestionBoxes(dialog)[0].click();
    await nextTick();
    expect(last(first.changes)).toBe("");

    order.return_suggestions = [...suggestions, { text: "Нет вылетов", category: "Прочее", confidence: "high", preselect: true, evidence: "" }];
    await nextTick();
    expect(last(first.changes)).toBe("");

    first.unmount();
    const second = mount(order);
    await nextTick();
    expect(last(second.changes)).toBe("");
    expect(second.host.querySelector(".return-badge")).toBeNull();
    expect(defaultReturnComment(order, orderIdentity(order))).toBe("");
  });

  it("keeps the operator's selection across a remount", async () => {
    const order = { order_id: "8", status: "warning", return_suggestions: suggestions };
    const first = mount(order);
    const dialog = await openDialog(first.host);
    suggestionBoxes(dialog)[2].click();
    await nextTick();
    first.unmount();
    const second = mount(order);
    await nextTick();
    expect(last(second.changes)).toBe("1) Низкое разрешение изображений;\n2) Макет в RGB.");
  });

  it("resets the selection after a prepared return", async () => {
    const order = reactive({ order_id: "9", status: "error", return_suggestions: suggestions });
    const { host, changes } = mount(order);
    await nextTick();
    expect(last(changes)).toBe("Низкое разрешение изображений.");
    order.action_result = { action: "reject", status: "prepared" };
    await nextTick();
    expect(last(changes)).toBe("");
    expect(host.querySelector(".return-badge")).toBeNull();
  });
});

describe("defaultReturnComment", () => {
  it("builds the comment from high-confidence suggestions of an unmounted order", () => {
    const order = { order_id: "10", status: "error", return_suggestions: suggestions };
    expect(defaultReturnComment(order, orderIdentity(order))).toBe("Низкое разрешение изображений.");
    expect(defaultReturnComment({ ...order, status: "passed" }, "x")).toBe("");
  });
});
