import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

const view = readFileSync("src/views/HistoryView.vue", "utf8");

describe("HistoryView preview loading", () => {
  it("loads the compact preview directly", () => {
    expect(view).toContain(':href="preview.url"');
    expect(view).toContain(':src="preview.url"');
    expect(view).toContain('loading="lazy"');
    expect(view).toContain('decoding="async"');
  });
});
