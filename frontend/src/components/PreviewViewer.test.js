import { afterEach, describe, expect, it, vi } from "vitest";
import { createApp, nextTick } from "vue";
import PreviewViewer from "./PreviewViewer.vue";

const mounted = [];
let calls = [];
let id = 0;
function mount(files) {
  const host = document.createElement("div");
  document.body.append(host);
  const app = createApp(PreviewViewer, { files });
  app.mount(host);
  mounted.push(() => { app.unmount(); host.remove(); });
  return host;
}
const ok = () => Promise.resolve({ ok: true, status: 200, blob: async () => new Blob(["preview"]) });
afterEach(() => {
  mounted.splice(0).forEach((unmount) => unmount());
  calls = [];
  id = 0;
  vi.unstubAllGlobals();
});

describe("PreviewViewer", () => {
  it("keeps thumbnail and full PDF rendition URLs page-specific", async () => {
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => `blob:viewer-${++id}`), revokeObjectURL: vi.fn() });
    vi.stubGlobal("fetch", vi.fn((src) => { calls.push(src); return ok(); }));
    const host = mount([{
      id: "file-1", filename: "two-page.pdf", page_count: 2,
      preview_paths: ["face.png", "back.png"],
      thumbnail_url: "/api/files/file-1/preview?size=thumbnail",
      full_preview_url: "/api/files/file-1/preview?size=full",
    }]);
    await nextTick();
    await Promise.resolve();
    expect(calls).toEqual([
      "/api/files/file-1/preview?size=thumbnail&page=1",
      "/api/files/file-1/preview?size=thumbnail&page=2",
    ]);
    host.querySelector(".preview-image").click();
    await nextTick();
    await Promise.resolve();
    expect(calls).toContain("/api/files/file-1/preview?size=full&page=1");
  });
});

describe("PreviewViewer missing side", () => {
  const backOnly = { id: "b", filename: "x_back.pdf", side: "back", parsed: { side: "back", back_colors: 4 }, width_mm: 301, height_mm: 214, thumbnail_url: "/b.png" };
  const faceOnly = { id: "f", filename: "x_face.pdf", side: "face", parsed: { side: "face", back_colors: 4 }, thumbnail_url: "/f.png" };

  it("shows a blank face sheet instead of putting the back file into the face slot", async () => {
    vi.stubGlobal("fetch", vi.fn(ok));
    const host = mount([backOnly]);
    await nextTick();
    const cells = [...host.querySelectorAll(".preview-cell")];
    expect(cells.map((cell) => cell.querySelector(".preview-label b").textContent)).toEqual(["Face", "Back"]);
    expect(cells[0].querySelector(".preview-missing-sheet")?.textContent).toBe("Нет файла face");
    expect(cells[0].querySelector(".preview-missing-sheet").style.aspectRatio).toBe("301 / 214");
    expect(cells[1].querySelector(".preview-filename").textContent).toBe("x_back.pdf");
  });

  it("shows a blank back sheet for a duplex order without its back file", async () => {
    vi.stubGlobal("fetch", vi.fn(ok));
    const host = mount([faceOnly]);
    await nextTick();
    const cells = [...host.querySelectorAll(".preview-cell")];
    expect(cells).toHaveLength(2);
    expect(cells[1].querySelector(".preview-missing-sheet")?.textContent).toBe("Нет файла back");
  });

  it("keeps a single slot for a one-sided order", async () => {
    vi.stubGlobal("fetch", vi.fn(ok));
    const host = mount([{ ...faceOnly, parsed: { side: "face", back_colors: 0 } }]);
    await nextTick();
    expect(host.querySelectorAll(".preview-cell")).toHaveLength(1);
    expect(host.querySelector(".preview-missing-sheet")).toBeNull();
  });
});
