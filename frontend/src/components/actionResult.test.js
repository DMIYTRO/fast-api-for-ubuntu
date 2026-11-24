import { describe, expect, it } from "vitest";
import { LAYERED_REVIEW_TEXT, LAYERED_REVIEW_TOAST, actionResultView, actionToast } from "./actionResult.js";

describe("actionResultView", () => {
  it("never shows raw English statuses", () => {
    for (const status of ["prepared", "pending", "review_required", "conflict", "error", "failed", "rejected", "not_found", "weird"]) {
      const view = actionResultView({ status, action: "print" });
      expect(view.text).not.toMatch(/\b(prepared|pending|review_required|conflict|error|failed|rejected|not_found|weird)\b/);
    }
  });

  it("maps statuses to tones", () => {
    expect(actionResultView({ status: "prepared" })).toMatchObject({ tone: "ok", text: "Подготовлено" });
    expect(actionResultView({ status: "review_required", message: "Созданы PDF…" })).toMatchObject({ tone: "attention", text: LAYERED_REVIEW_TEXT });
    expect(actionResultView({ status: "conflict", message: "Файл уже есть" })).toMatchObject({ tone: "attention", text: "Файл уже есть" });
    expect(actionResultView({ status: "rejected", message: "Нет PDF" })).toMatchObject({ tone: "error", text: "Нет PDF" });
    expect(actionResultView({ status: "failed" }).tone).toBe("error");
    expect(actionResultView({ status: "failed" }).text).toContain("причина не сохранена");
  });
});

describe("actionToast", () => {
  it("does not count review_required as a failure", () => {
    const toast = actionToast([{ order_id: "1", status: "review_required", message: "Созданы PDF" }], "print");
    expect(toast).toBe(LAYERED_REVIEW_TOAST);
    expect(toast).not.toContain("Не удалось");
  });

  it("reports real failures with an operator message", () => {
    expect(actionToast([{ status: "review_required" }, { status: "error", message: "Нет исходника" }], "print"))
      .toBe("Не удалось обработать 1 заказ(а): Нет исходника");
    expect(actionToast([{ status: "failed" }], "print")).not.toMatch(/failed/);
  });

  it("keeps the success messages", () => {
    expect(actionToast([{ status: "prepared" }], "print")).toBe("Задание на печать подготовлено");
    expect(actionToast([{ status: "prepared" }], "reject")).toBe("Возврат подготовлен");
  });
});
