// Usage counts and load times, sent to PostHog as events of our own,
// without its script, cookies, or storage of any kind:
// each page load has a random ID of its own, which is never kept, so visits can't be tied together,
// and nothing says where anyone is.

const HOST = "https://us.i.posthog.com";
// Public by design: it can only send events, not read them.
const PROJECT_TOKEN = "phc_yeU63ZoiN2rE5nwYzRF5RTS4moUiCStTsYjqxck4G4sA";

// Not from the dev server, nor from automated browsers, as for the screenshots and end-to-end tests,
// nor for visitors whose browser asks not to be tracked.
const enabled =
  !import.meta.env.DEV &&
  !navigator.webdriver &&
  (navigator as { readonly globalPrivacyControl?: boolean }).globalPrivacyControl !== true &&
  navigator.doNotTrack !== "1";

// A UUIDv7, as PostHog wants its sessions' IDs to be: the time in milliseconds, then random bits.
function uuidv7(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  let ms = Date.now();
  for (let i = 5; i >= 0; i--) {
    bytes[i] = ms % 256;
    ms = Math.floor(ms / 256);
  }
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x70;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  const hex = [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}

// Both the visitor and the session, as far as PostHog is told, for this page load only.
const pageLoad = uuidv7();

type Properties = Readonly<Record<string, unknown>>;

// What the page sends with every event, as what it's showing, set by the page.
let context = (): Properties => ({});
export function setContext(get: () => Properties): void {
  context = get;
}

// Sent together a little after the first of them, so that a burst of clicks is one request.
const FLUSH_MS = 2000;
const queue: object[] = [];
let flushTimeout: number | undefined;

export function capture(event: string, properties: Properties = {}): void {
  if (!enabled) {
    return;
  }
  queue.push({
    event,
    distinct_id: pageLoad,
    timestamp: new Date().toISOString(),
    properties: {
      $session_id: pageLoad,
      // Not making a person of each page load, which PostHog would otherwise keep a profile for.
      $process_person_profile: false,
      $current_url: location.href,
      $host: location.host,
      $pathname: location.pathname,
      $raw_user_agent: navigator.userAgent,
      $screen_width: screen.width,
      $screen_height: screen.height,
      $viewport_width: innerWidth,
      $viewport_height: innerHeight,
      embedded: window.parent !== window,
      ...context(),
      ...properties,
    },
  });
  flushTimeout ??= window.setTimeout(flush, FLUSH_MS);
}

function flush(): void {
  clearTimeout(flushTimeout);
  flushTimeout = undefined;
  if (queue.length === 0) {
    return;
  }
  const batch = queue.splice(0);
  // `keepalive`, so that it's still sent as the page closes.
  // As plain text, the default for a string, so that the browser needn't ask PostHog first whether it may.
  fetch(`${HOST}/batch/`, {
    method: "POST",
    body: JSON.stringify({ api_key: PROJECT_TOKEN, batch }),
    keepalive: true,
  }).catch(() => {
    // Lost, as when offline or blocked.
  });
}

// The page viewed, with where the visitor came from, as PostHog's web analytics reads it.
export function capturePageview(properties: Properties = {}): void {
  const referrer = document.referrer;
  let referringDomain = "$direct";
  try {
    referringDomain = referrer === "" ? "$direct" : new URL(referrer).host;
  } catch {
    // Not a URL, so said as it is.
  }
  const params = new URLSearchParams(location.search);
  const campaign = Object.fromEntries(
    ["utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "ref"].flatMap(
      (name) => {
        const value = params.get(name);
        return value === null ? [] : [[name, value]];
      },
    ),
  );
  capture("$pageview", {
    $referrer: referrer === "" ? "$direct" : referrer,
    $referring_domain: referringDomain,
    ...campaign,
    ...properties,
  });
}

// What the page loaded, by kind, summed until it's next hidden:
// its own files by their folder or name, as `routes` for `data/routes/H03.bin`, and others' by host, as the map's tiles.
interface Loads {
  count: number;
  // Fetched again, rather than read from the browser's cache, as far as the browser says,
  // which for other sites' files, as the map's tiles, it doesn't, unless they let it.
  fetched: number;
  failed: number;
  bytes: number;
  totalMs: number;
  maxMs: number;
}
const loads = new Map<string, Loads>();

function loadKind(url: URL): string {
  if (url.origin !== location.origin) {
    return url.host;
  }
  const dataAt = url.pathname.indexOf("/data/");
  if (dataAt < 0) {
    return "assets";
  }
  const [first = ""] = url.pathname.slice(dataAt + "/data/".length).split("/");
  return first;
}

// The slowest tap, click, or key press since the page was last hidden, from it to the next frame drawn after it.
let slowestInteraction: { readonly ms: number; readonly name: string } | undefined;

if (enabled) {
  // Observed rather than read from the browser's list of them, which stops at 250.
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (!(entry instanceof PerformanceResourceTiming)) {
        continue;
      }
      const url = new URL(entry.name);
      // Not the events themselves.
      if (url.origin === HOST) {
        continue;
      }
      const kind = loadKind(url);
      const summed = loads.get(kind) ?? {
        count: 0,
        fetched: 0,
        failed: 0,
        bytes: 0,
        totalMs: 0,
        maxMs: 0,
      };
      summed.count += 1;
      summed.fetched += entry.transferSize > 0 ? 1 : 0;
      summed.failed += entry.responseStatus >= 400 ? 1 : 0;
      summed.bytes += entry.transferSize;
      summed.totalMs += entry.duration;
      summed.maxMs = Math.max(summed.maxMs, entry.duration);
      loads.set(kind, summed);
    }
  }).observe({ type: "resource", buffered: true });

  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (
        entry instanceof PerformanceEventTiming &&
        entry.interactionId > 0 &&
        entry.duration > (slowestInteraction?.ms ?? 0)
      ) {
        slowestInteraction = { ms: entry.duration, name: entry.name };
      }
    }
    // The shortest the browser allows, as only the slowest is kept anyway.
  }).observe({ type: "event", durationThreshold: 16, buffered: true } as PerformanceObserverInit);

  // Links out, as to QueensLink or the code.
  document.addEventListener("click", (event) => {
    const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
    if (link instanceof HTMLAnchorElement && new URL(link.href).origin !== location.origin) {
      capture("link_clicked", { href: link.href });
    }
  });

  // Since it was last shown, or since it was loaded.
  let shownAt = performance.now();
  // Whether it's been sent as hidden since it was last shown, so that it isn't sent twice.
  let sentHidden = false;
  const sendHidden = (): void => {
    if (sentHidden) {
      return;
    }
    sentHidden = true;
    const round = (ms: number): number => Math.round(ms);
    capture("page_hidden", {
      visible_seconds: Math.round((performance.now() - shownAt) / 1000),
      loads: Object.fromEntries(
        [...loads].map(([kind, summed]) => [
          kind,
          {
            count: summed.count,
            // Not said where the browser kept them from the page, rather than none being fetched.
            fetched: summed.bytes === 0 ? undefined : summed.fetched,
            failed: summed.failed,
            bytes: summed.bytes === 0 ? undefined : summed.bytes,
            mean_ms: round(summed.totalMs / summed.count),
            max_ms: round(summed.maxMs),
          },
        ]),
      ),
      slowest_interaction_ms:
        slowestInteraction === undefined ? undefined : round(slowestInteraction.ms),
      slowest_interaction: slowestInteraction?.name,
    });
    loads.clear();
    slowestInteraction = undefined;
  };
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "hidden") {
      shownAt = performance.now();
      sentHidden = false;
      return;
    }
    sendHidden();
    flush();
  });
  // Closed without being hidden first, as some browsers do; sent then, as there's no later.
  // Either may come first, as Safari has sent `pagehide` before `visibilitychange`.
  addEventListener("pagehide", () => {
    sendHidden();
    flush();
  });
}

// How long the page took to start, from the browser's own timings of it.
export function navigationTimings(): Properties {
  const [navigation] = performance.getEntriesByType("navigation");
  const connection = (navigator as { readonly connection?: { readonly effectiveType?: string } })
    .connection;
  return navigation instanceof PerformanceNavigationTiming
    ? {
        first_byte_ms: Math.round(navigation.responseStart),
        dom_ready_ms: Math.round(navigation.domContentLoadedEventEnd),
        connection: connection?.effectiveType,
      }
    : { connection: connection?.effectiveType };
}
