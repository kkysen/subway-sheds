import { copyFile, mkdir } from "node:fs/promises";
import { join } from "node:path";
import { defineConfig } from "vite";

// The pages beside the main page, each its own copy of it, as GitHub Pages serves only files.
// QueensLink's, the same as the main page for now, for queenslink.org to embed.
// Its scripts, styles, and data are all under the base path, so the same page works from there.
const pages = ["queenslink"];

let outDir = "dist";

export default defineConfig({
  plugins: [
    {
      name: "pages",
      apply: "build",
      configResolved(config) {
        outDir = config.build.outDir;
      },
      async closeBundle() {
        for (const page of pages) {
          await mkdir(join(outDir, page), { recursive: true });
          await copyFile(join(outDir, "index.html"), join(outDir, page, "index.html"));
        }
      },
    },
  ],
});
