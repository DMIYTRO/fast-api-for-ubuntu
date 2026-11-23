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

describe("ReturnReasons dialog layout", () => {
  const panelLines = (dialog) => [...dialog.querySelectorAll(".return-selected .return-line-text")].map((node) => node.textContent);
  async function typeCustom(dialog, value) {
    const textarea = dialog.querySelector(".return-custom textarea");
    textarea.value = value;
    textarea.dispatchEvent(new Event("input"));
    await nextTick();
  }

  it("shows the final comment lines, including the free text, in the selected panel", async () => {
    const { host, changes } = mount({ order_id: "20", status: "error", return_suggestions: suggestions });
    const dialog = await openDialog(host);
    expect(dialog.querySelector(".return-selected-title").textContent).toBe("Будет отправлено клиенту");
    expect(panelLines(dialog)).toEqual(["Низкое разрешение изображений."]);

    await typeCustom(dialog, "Проверьте шрифты");
    expect(panelLines(dialog)).toEqual(["1) Низкое разрешение изображений;", "2) Проверьте шрифты."]);
    expect(panelLines(dialog).join("\n")).toBe(last(changes));
    expect(dialog.querySelector(".return-line.custom .return-line-text").textContent).toBe("2) Проверьте шрифты.");
  });

  it("shows an empty state when nothing is selected", async () => {
    const { host } = mount({ order_id: "21", status: "passed" });
    const dialog = await openDialog(host);
    expect(dialog.querySelector(".return-lines")).toBeNull();
    expect(dialog.querySelector(".return-selected-empty").textContent).toContain("Пока ничего не выбрано");
  });

  it("removes a reason and the free text from the selected panel", async () => {
    const { host, changes } = mount({ order_id: "22", status: "error", return_suggestions: suggestions });
    const dialog = await openDialog(host);
    await typeCustom(dialog, "Свой текст");
    const [reasonRemove] = dialog.querySelectorAll(".return-line-remove");
    reasonRemove.click();
    await nextTick();
    expect(suggestionBoxes(dialog)[0].checked).toBe(false);
    expect(last(changes)).toBe("Свой текст.");

    dialog.querySelector(".return-line.custom .return-line-remove").click();
    await nextTick();
    expect(dialog.querySelector(".return-custom textarea").value).toBe("");
    expect(last(changes)).toBe("");
    expect(dialog.querySelector(".return-selected-empty")).not.toBeNull();
  });

  it("groups the catalogue by category, highlights selected rows and searches text and category", async () => {
    const { host } = mount({ order_id: "23", status: "error" });
    const dialog = await openDialog(host);
    const heads = () => [...dialog.querySelectorAll(".return-group-name")].map((node) => node.textContent);
    expect(heads()).toEqual(["Разрешение", "Размер", "Цвет", "Прочее"]);

    const row = [...dialog.querySelectorAll(".return-options .return-option")].find((node) => node.textContent.includes("Макет в RGB"));
    row.querySelector("input").click();
    await nextTick();
    expect(row.classList.contains("is-selected")).toBe(true);
    expect(dialog.querySelector(".return-group-selected").textContent).toContain("1");

    const input = dialog.querySelector(".return-search input");
    input.value = "размер";
    input.dispatchEvent(new Event("input"));
    await nextTick();
    expect(heads()).toEqual(["Размер"]);
    input.value = "цвет";
    input.dispatchEvent(new Event("input"));
    await nextTick();
    expect(heads()).toEqual(["Цвет"]);
    expect(dialog.querySelector(".return-found").textContent).toBe("Найдено: 1");
  });

  it("collapses a large catalogue by category and expands a group on click", async () => {
    const big = Array.from({ length: 70 }, (_, index) => ({ category: `Категория ${index % 7}`, text: `Причина ${index}` }));
    const host = document.createElement("div");
    document.body.append(host);
    const app = createApp(ReturnReasons, { order: { order_id: "24", status: "error" }, reasons: big });
    app.mount(host);
    mounted.push(() => { app.unmount(); host.remove(); });
    const dialog = await openDialog(host);
    const groups = [...dialog.querySelectorAll(".return-group")];
    expect(groups).toHaveLength(7);
    expect(groups.every((group) => group.querySelector(".return-group-items").style.display === "none")).toBe(true);
    groups[2].querySelector(".return-group-head").click();
    await nextTick();
    expect(groups[2].querySelector(".return-group-items").style.display).toBe("");
    expect(groups[2].querySelector(".return-group-head").getAttribute("aria-expanded")).toBe("true");
  });

  it("closes on Escape", async () => {
    const { host } = mount({ order_id: "25", status: "error" });
    await openDialog(host);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    await nextTick();
    expect(document.querySelector(".return-dialog")).toBeNull();
  });
});

describe("ReturnReasons card summary", () => {
  it("shows the first reason and the number of further lines next to the trigger", async () => {
    const { host } = mount({ order_id: "26", status: "error", return_suggestions: suggestions });
    await nextTick();
    expect(host.querySelector(".return-summary-text").textContent).toBe("Низкое разрешение изображений");
    expect(host.querySelector(".return-summary-more")).toBeNull();

    const dialog = await openDialog(host);
    suggestionBoxes(dialog)[1].click();
    await nextTick();
    expect(host.querySelector(".return-summary-more").textContent).toBe("+1");
    expect(host.querySelector(".return-summary").title).toContain("2) Неверный размер макета.");
  });

  it("hides the summary when nothing is selected", async () => {
    const { host } = mount({ order_id: "27", status: "passed", return_suggestions: suggestions });
    await nextTick();
    expect(host.querySelector(".return-summary")).toBeNull();
  });
});

describe("defaultReturnComment", () => {
  it("builds the comment from high-confidence suggestions of an unmounted order", () => {
    const order = { order_id: "10", status: "error", return_suggestions: suggestions };
    expect(defaultReturnComment(order, orderIdentity(order))).toBe("Низкое разрешение изображений.");
    expect(defaultReturnComment({ ...order, status: "passed" }, "x")).toBe("");
  });
});
