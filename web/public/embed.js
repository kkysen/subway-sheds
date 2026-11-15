// For a site that embeds the map in an `<iframe>`, to paste beside it:
//
//     <script src="https://kkysen.github.io/subway-sheds/embed.js" defer></script>
//
// Keeps whether its explainer was dismissed in the embedding site's own storage,
// as the browser can block or clear the map's own in an `<iframe>`, as Safari does,
// and tells the map when it asks or loads.
(() => {
  // Where the map is, as this is loaded from beside it.
  const script = document.currentScript;
  if (!(script instanceof HTMLScriptElement)) {
    return;
  }
  const origin = new URL(script.src).origin;
  const ASK = "subway-sheds:explainer?";
  const SAY = "subway-sheds:explainer";
  // Only names `main.ts` uses, rather than anything a message says.
  const isName = (name) => typeof name === "string" && /^[a-z-]+$/.test(name);
  const key = (name) => `subway-sheds:${name}`;

  function dismissed(name) {
    try {
      return localStorage.getItem(key(name)) === "dismissed";
    } catch {
      // Storage can be unavailable, as in some private windows.
      return false;
    }
  }

  // The map's frames, rather than any other's on the page.
  function maps() {
    return [...document.querySelectorAll("iframe")].filter((frame) => {
      try {
        return new URL(frame.src, location.href).origin === origin;
      } catch {
        return false;
      }
    });
  }

  function tell(frame, name) {
    frame.contentWindow?.postMessage({ type: SAY, name, dismissed: dismissed(name) }, origin);
  }

  window.addEventListener("message", (event) => {
    const frame = maps().find((frame) => frame.contentWindow === event.source);
    const data = event.data;
    if (
      event.origin !== origin ||
      frame === undefined ||
      typeof data !== "object" ||
      data === null
    ) {
      return;
    }
    if (!isName(data.name)) {
      return;
    }
    if (data.type === ASK) {
      tell(frame, data.name);
    } else if (data.type === SAY && data.dismissed === true) {
      try {
        localStorage.setItem(key(data.name), "dismissed");
      } catch {
        // Not saved, as when storage is unavailable.
      }
    }
  });

  // Also now and when the map's loaded, in case it asked before this was listening.
  // Before the map's there, the browser drops it, as it's not yet from `origin`.
  for (const frame of maps()) {
    tell(frame, "explainer");
    frame.addEventListener("load", () => tell(frame, "explainer"));
  }
})();
