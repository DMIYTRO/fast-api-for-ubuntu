import { afterEach, expect, it } from "vitest";
import { createApp } from "vue";
import FileParameters from "./FileParameters.vue";
let app;
let host;
afterEach(() => { app?.unmount(); host?.remove(); });
const mount = (file) => {
  host = document.createElement("div"); document.body.append(host);
  app = createApp(FileParameters, { file }); app.mount(host);
};
it("shows the original DPI of a downsampled raster", () => {
  mount({ filename: "a.tif", dpi_x: 1200, dpi_y: 1200, downsampled_from_dpi: 1200 });
  expect(host.textContent).toContain("Уменьшено с");
  expect(host.textContent).toContain("1200.0 DPI");
});
it("hides the downsample row for ordinary files", () => {
  mount({ filename: "a.tif", dpi_x: 300, dpi_y: 300, downsampled_from_dpi: null });
  expect(host.textContent).not.toContain("Уменьшено с");
});
