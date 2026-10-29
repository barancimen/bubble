// Serves dashboard.html, and nothing else, at gestaltschaft.com/bubble (a port of serve_dashboard.py).
import page from "../public/dashboard.html";

const STALE_HOURS = 30; // the page is rebuilt twice a day; older than this means a run or an upload failed

// The page has no scripts, images or forms, so the browser is told to refuse all of them.
const SECURITY_HEADERS = {
  "Content-Security-Policy":
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Referrer-Policy": "no-referrer",
  "Cache-Control": "no-cache",
};

// When bubble_watch.py fetched the data (its <meta name='generated'>); NaN if missing.
const GENERATED = Date.parse((page.match(/<meta name='generated' content='([^']+)'>/) || [])[1]);

export default {
  fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/bubble/") {
      return Response.redirect(`${url.origin}/bubble${url.search}`, 301);
    }
    if (url.pathname !== "/bubble") {
      return new Response("Not found", { status: 404, headers: SECURITY_HEADERS });
    }
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method not allowed", { status: 405, headers: { ...SECURITY_HEADERS, Allow: "GET, HEAD" } });
    }
    let body = page;
    const age = (Date.now() - GENERATED) / 3.6e6;
    if (!(age <= STALE_HOURS)) { // say so on the page itself, so nobody reads old numbers as today's
      const when = Number.isNaN(age) ? "at an unknown time" : `${Math.round(age)} hours ago`;
      body = body.replace("<body>", '<body><p style="border:2px solid #b00020;padding:8px;font-weight:700">' +
        `This page was last rebuilt ${when}: the update has failed, so these numbers may be out of date.</p>`);
    }
    return new Response(request.method === "HEAD" ? null : body, {
      headers: { ...SECURITY_HEADERS, "Content-Type": "text/html; charset=utf-8" },
    });
  },
};
