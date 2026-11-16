import { mkdir, readFile, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { defineConfig } from "vite";

// The pages beside the main page, each its own copy of it, as GitHub Pages serves only files.
// QueensLink's, for queenslink.org to embed.
// Its scripts, styles, and data are all under the base path, so the same page works from there.
const pages = ["queenslink"];

// What's only on one page, in `index.html`: between `<!-- page: <name> -->` and `<!-- /page -->`,
// each on a line of its own, with `main` for the main page.
const pageBlock = /^[ \t]*<!-- page: (\w+) -->\n([\s\S]*?)^[ \t]*<!-- \/page -->\n/gm;

// `html` as `page` has it: with only its own blocks, and without their markers.
function forPage(html: string, page: string): string {
  return html.replace(pageBlock, (_block, only: string, inner: string) =>
    only === page ? inner : "",
  );
}

// The page a dev server's request is for, by the first part of its path, as built.
function requestedPage(url: string, base: string): string {
  const [first = ""] = url.slice(base.length).split(/[/?#]/);
  return pages.includes(first) ? first : "main";
}

let outDir = "dist";
let base = "/";
let building = false;

export default defineConfig({
  plugins: [
    {
      name: "pages",
      configResolved(config) {
        outDir = config.build.outDir;
        base = config.base;
        building = config.command === "build";
      },
      // Built, it's left with every page's blocks, for `closeBundle` to make each page from it.
      transformIndexHtml(html, { originalUrl, server }) {
        return server === undefined
          ? html
          : forPage(html, requestedPage(originalUrl ?? base, base));
      },
      // Also called as the dev server stops, when there's nothing built.
      async closeBundle() {
        if (!building) {
          return;
        }
        const html = await readFile(join(outDir, "index.html"), "utf8");
        for (const page of pages) {
          await mkdir(join(outDir, page), { recursive: true });
          await writeFile(join(outDir, page, "index.html"), forPage(html, page));
        }
        await writeFile(join(outDir, "index.html"), forPage(html, "main"));
      },
    },
  ],
});
