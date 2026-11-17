import { afterEach, describe, expect, it } from "vitest";
import { createApp, nextTick } from "vue";
import CorrectionDecision from "./CorrectionDecision.vue";

const mounted = [];
function mount(order) {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(CorrectionDecision, { order, busy: false });
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return host;
}
afterEach(() => mounted.splice(0).forEach((unmount) => unmount()));

const face = { name: "face.pdf", resample_decision: "accept", callas_source_fix: { status: "accepted" } };
const back = { name: "back.pdf", resample_decision: "ask_confirmation" };

describe("CorrectionDecision", () => {
  it("names the files the decision still applies to when some sides use a Callas fix", async () => {
    const host = mount({ status: "waiting_confirmation", pending_confirmations: 1, files: [face, back] });
    await nextTick();
    expect(host.textContent).toContain("Требуется решение по коррекции");
    const scope = host.querySelector(".decision-scope").textContent;
    expect(scope).toContain("исправленного Callas: face.pdf");
    expect(scope).toContain("относятся только к файлам: back.pdf");
    expect([...host.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Применить", "Оставить без изменений"]);
  });

  it("keeps the plain decision without accepted fixes and hides once the order resumed", async () => {
    const plain = mount({ status: "waiting_confirmation", files: [{ ...back, name: "face.pdf" }, back] });
    await nextTick();
    expect(plain.querySelector(".decision-box")).not.toBeNull();
    expect(plain.querySelector(".decision-scope")).toBeNull();

    const resumed = mount({ status: "correction_confirmed", files: [face, { ...face, name: "back.pdf" }] });
    await nextTick();
    expect(resumed.querySelector(".decision-box")).toBeNull();
  });
});
