// Reports video/manifest requests from any tab to the local Multitool server.
const SERVER = "http://127.0.0.1:8765";
const MEDIA_PATH = /\.(m3u8|mpd|mp4|webm|mov|mkv)$|\/videoplayback$/i;
const MEDIA_TYPE = /^video\/|mpegurl|dash\+xml|yt-ump/i;
const SEGMENT = /\.(ts|m4s|m4a|aac)$/i;
// Skips the burst of requests one video makes, but reports it again after 30 s (e.g. once the list was cleared).
const seen = new Map();

chrome.webRequest.onHeadersReceived.addListener(
  async (d) => {
    if (d.tabId < 0) return;
    const u = new URL(d.url);
    const type = d.responseHeaders?.find((h) => h.name.toLowerCase() === "content-type")?.value || "";
    if (SEGMENT.test(u.pathname) || type.includes("mp2t")) return;
    if (!MEDIA_PATH.test(u.pathname) && !MEDIA_TYPE.test(type)) return;

    const tab = await chrome.tabs.get(d.tabId).catch(() => null);
    if (!tab?.url?.startsWith("http") || tab.url.startsWith(SERVER)) return;

    // Instagram/Facebook fetch byte ranges; drop them so the fallback grabs the whole file.
    u.searchParams.delete("bytestart");
    u.searchParams.delete("byteend");
    const key = tab.url + u.pathname + tab.title; // title too: YouTube retitles the tab after navigating
    if (Date.now() - (seen.get(key) || 0) < 30000) return;
    seen.set(key, Date.now());

    fetch(SERVER + "/sniffed", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page: tab.url, media: u.href, title: tab.title }),
    }).catch(() => {});
  },
  { urls: ["<all_urls>"] },
  ["responseHeaders"]
);

// Chrome encrypts its cookie DB so yt-dlp can't read it; hand the server the video sites' cookies
// so login/bot walls (YouTube, Instagram, X…) pass. Only these domains leave the browser.
const COOKIE_DOMAINS = ["youtube.com", "google.com", "x.com", "twitter.com", "instagram.com", "tiktok.com",
  "facebook.com", "reddit.com", "vimeo.com", "twitch.tv"];

async function syncCookies() {
  const cookies = (await Promise.all(COOKIE_DOMAINS.map((domain) => chrome.cookies.getAll({ domain })))).flat();
  fetch(SERVER + "/cookies", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cookies }),
  }).catch(() => {});
}
// Sites rotate session cookies (YouTube every few minutes), so resync shortly after any change.
let syncTimer;
chrome.cookies.onChanged.addListener(({ cookie }) => {
  const domain = "." + cookie.domain.replace(/^\./, "");
  if (!COOKIE_DOMAINS.some((d) => domain.endsWith("." + d))) return;
  clearTimeout(syncTimer);
  syncTimer = setTimeout(syncCookies, 2000);
});
syncCookies(); // also on every service-worker wake

// Multitool pages already open when the extension is installed get marked too (new ones get mark.js on load),
// so the setup box closes right away.
chrome.runtime.onInstalled.addListener(async () => {
  for (const tab of await chrome.tabs.query({ url: ["http://127.0.0.1:8765/*", "http://localhost:8765/*"] }))
    chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["mark.js"] }).catch(() => {});
});
