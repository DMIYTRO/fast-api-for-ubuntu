import { afterEach, describe, expect, it } from "vitest";
import { createApp, nextTick } from "vue";
import StartCheckPanel from "./StartCheckPanel.vue";

const mounted = [];
function mount(props = {}) {
  const host = document.createElement("div");
  document.body.append(host);
  const started = [];
  const app = createApp(StartCheckPanel, {
    open: true,
    busy: false,
    config: { default_input_path: "/in", profiles: [{ id: "digital", name: "Цифровая печать" }] },
    onStart: (payload) => started.push(payload),
    ...props,
  });
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return { started };
}
const radios = () => [...document.body.querySelectorAll("fieldset input[type=radio]")];
const labelOf = (radio) => radio.closest("label").textContent.trim();

afterEach(() => mounted.splice(0).forEach((unmount) => unmount()));

describe("StartCheckPanel correction policy", () => {
  it("shows three policy radios, ask selected by default", () => {
    mount();
    const items = radios();
    expect(items.map((r) => r.value)).toEqual(["ask", "auto", "reject"]);
    expect(items.find((r) => r.checked).value).toBe("ask");
  });

  it("mentions the configured limit in the auto label", () => {
    mount({ config: { default_input_path: "/in", auto_correct_max_mm: 2.5, correction_confirm_max_mm: 7 } });
    const auto = radios().find((r) => r.value === "auto");
    expect(labelOf(auto)).toContain("Автоматически принимать малые коррекции");
    expect(labelOf(auto)).toContain("(поля, до 2,5 мм обрезки");
    expect(labelOf(auto)).toContain("до 7 мм — с подтверждением");
  });

  it("hints the correction order in the auto label tooltip", () => {
    mount();
    const title = radios().find((r) => r.value === "auto").closest("label").getAttribute("title");
    expect(title).toContain("открыть скрытые поля → дорисовать поля Callas → растянуть (PDF) → сжать → подогнать");
  });

  it("falls back to 4 mm when the server does not report a limit", () => {
    mount();
    expect(labelOf(radios().find((r) => r.value === "auto"))).toContain("до 4 мм обрезки; до 10 мм");
  });

  it("choosing auto sends approve_corrections and policy auto", async () => {
    const { started } = mount();
    const auto = radios().find((r) => r.value === "auto");
    auto.checked = true;
    auto.dispatchEvent(new Event("change"));
    await nextTick();
    document.body.querySelector("form").dispatchEvent(new Event("submit"));
    await nextTick();
    expect(started).toHaveLength(1);
    expect(started[0]).toMatchObject({ correction_policy: "auto", approve_corrections: true });
  });

  it("ask keeps approve_corrections off", async () => {
    const { started } = mount();
    document.body.querySelector("form").dispatchEvent(new Event("submit"));
    await nextTick();
    expect(started[0]).toMatchObject({ correction_policy: "ask", approve_corrections: false });
  });
});

describe("StartCheckPanel check policy", () => {
  const checkbox = () => [...document.body.querySelectorAll("input[type=checkbox]")].find((input) => input.closest("label").textContent.includes("Полная проверка для всех файлов"));

  it("sends check_policy auto by default and shows the hint", async () => {
    const { started } = mount();
    expect(document.body.textContent).toContain("Иначе файлы из папки Input проверяются облегчённо, из CorelDRAW — полностью");
    expect(checkbox().checked).toBe(false);
    document.body.querySelector("form").dispatchEvent(new Event("submit"));
    await nextTick();
    expect(started[0].check_policy).toBe("auto");
  });

  it("sends check_policy full when the checkbox is ticked", async () => {
    const { started } = mount();
    const box = checkbox();
    box.checked = true;
    box.dispatchEvent(new Event("change"));
    await nextTick();
    document.body.querySelector("form").dispatchEvent(new Event("submit"));
    await nextTick();
    expect(started[0]).toMatchObject({ check_policy: "full", correction_policy: "ask" });
  });
});
