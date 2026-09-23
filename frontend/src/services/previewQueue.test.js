import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { previewQueue } from "./previewQueue.js";

const orders = (...urls) => [{ files: urls.map((preview_url) => ({ preview_url })) }];

describe("preview queue", () => {
  const originalImage = globalThis.Image;
  let images;

  beforeEach(() => {
    images = [];
    globalThis.Image = class {
      constructor() { images.push(this); }
      set src(value) { this.url = value; }
    };
  });

  afterEach(() => {
    previewQueue.clear();
    globalThis.Image = originalImage;
    vi.restoreAllMocks();
  });

  it("prefetches at most two and gives the active page the next slot", () => {
    previewQueue.enqueue(orders("a", "b", "c"));
    expect(images.map((image) => image.url)).toEqual(["a", "b"]);

    previewQueue.prioritize(orders("d", "c"));
    images[0].onload();
    expect(images[2].url).toBe("d");
    images[1].onload();
    expect(images[3].url).toBe("c");
  });

  it("waits until all images on a page finish before continuing", async () => {
    previewQueue.enqueue(orders("first", "second"));
    let completed = false;
    const wait = previewQueue.whenIdle().then(() => { completed = true; });
    images[0].onload();
    await Promise.resolve();
    expect(completed).toBe(false);
    images[1].onload();
    await wait;
    expect(completed).toBe(true);
  });

  it("allows a failed preview to be retried after it becomes ready", () => {
    previewQueue.enqueue(orders("pending"));
    images[0].onerror();
    previewQueue.prioritize(orders("pending"));
    expect(images[1].url).toBe("pending");
  });
});
