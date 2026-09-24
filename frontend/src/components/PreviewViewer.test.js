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
