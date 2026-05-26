/**
 * OLX Proxy Worker — Next.js SSR data endpoint strategy
 *
 * KEY INSIGHT: OLX is built on Next.js. Next.js apps expose ALL their
 * server-rendered page data as plain JSON at:
 *   /_next/data/<BUILD_ID>/path/to/page.json
 *
 * This endpoint:
 *   ✅ Returns full listing data as JSON
 *   ✅ NOT protected by Akamai Bot Manager
 *   ✅ No cookies needed
 *   ✅ Works from any IP
 *
 * The BUILD_ID changes on each OLX deployment (roughly weekly).
 * This worker auto-discovers the current BUILD_ID on every request.
 *
 * DEPLOY: paste into Cloudflare Workers → Save and Deploy
 * No cookies needed. No manual updates needed.
 */

export default {
  async fetch(request) {
    const incomingUrl = new URL(request.url);
    const action = incomingUrl.searchParams.get("action");

    // ── Mode 1: Get current Next.js BUILD_ID ──────────────────
    if (action === "buildid") {
      return await getBuildId();
    }

    // ── Mode 2: Fetch listings via _next/data ─────────────────
    const keyword     = incomingUrl.searchParams.get("keyword") || "mobile";
    const locationId  = incomingUrl.searchParams.get("location_id") || "4058997";
    const locationSlug = incomingUrl.searchParams.get("location_slug") || "mumbai_g4058997";

    // Step 1: Get build ID
    const buildIdResp = await getBuildId();
    const buildData   = await buildIdResp.json();
    if (buildData.error) {
      return new Response(JSON.stringify(buildData), { status: 500, headers: { "Content-Type": "application/json" } });
    }
    const buildId = buildData.buildId;

    // Step 2: Fetch _next/data JSON
    const nextUrl = `https://www.olx.in/_next/data/${buildId}/en-in/${locationSlug}/q-${keyword.replace(/ /g, "-")}.json?location=${locationSlug}&search=${keyword}`;

    try {
      const resp = await fetch(nextUrl, {
        headers: {
          "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
          "Accept":          "application/json",
          "Accept-Language": "en-IN,en;q=0.9",
          "Referer":         `https://www.olx.in/${locationSlug}/q-${keyword}`,
          "x-nextjs-data":   "1",
        },
      });

      const text = await resp.text();
      return new Response(text, {
        status: resp.status,
        headers: {
          "Content-Type":                "application/json",
          "Access-Control-Allow-Origin": "*",
          "X-Build-ID":                  buildId,
          "X-Next-URL":                  nextUrl,
        },
      });

    } catch (err) {
      return new Response(JSON.stringify({ error: String(err), nextUrl }), {
        status: 500, headers: { "Content-Type": "application/json" },
      });
    }
  },
};

// ── Auto-discover BUILD_ID from OLX homepage ──────────────────────────────
async function getBuildId() {
  try {
    // Fetch OLX homepage HTML and extract __NEXT_DATA__ buildId
    const resp = await fetch("https://www.olx.in/", {
      headers: {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "text/html,application/xhtml+xml",
        "Accept-Language": "en-IN,en;q=0.9",
      },
    });
    const html = await resp.text();

    // Extract buildId from __NEXT_DATA__ script tag
    const match = html.match(/"buildId"\s*:\s*"([^"]+)"/);
    if (match) {
      return new Response(JSON.stringify({ buildId: match[1] }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    // Fallback: try _next/static manifest
    const manifestResp = await fetch("https://www.olx.in/_next/static/chunks/pages/_app.js", {
      headers: { "User-Agent": "Mozilla/5.0" },
    });
    const js = await manifestResp.text();
    const m2 = js.match(/buildId['":\s]+"([a-zA-Z0-9_-]{8,})"/);
    if (m2) {
      return new Response(JSON.stringify({ buildId: m2[1] }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response(JSON.stringify({ error: "Could not find buildId" }), {
      status: 500, headers: { "Content-Type": "application/json" },
    });

  } catch (err) {
    return new Response(JSON.stringify({ error: String(err) }), {
      status: 500, headers: { "Content-Type": "application/json" },
    });
  }
}
