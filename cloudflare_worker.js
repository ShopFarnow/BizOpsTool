/**
 * OLX Proxy Worker — Final Version
 * Fetches OLX HTML page, extracts window.__APP listing data
 * No cookies, no buildId, no API keys needed.
 *
 * DEPLOY: Cloudflare Workers → paste → Save and Deploy
 * URL format: https://your-worker.workers.dev/?keyword=mobile&location_slug=mumbai_g4058997
 */
export default {
  async fetch(request) {
    const url     = new URL(request.url);
    const keyword = url.searchParams.get("keyword") || "mobile";
    const locSlug = url.searchParams.get("location_slug") || "mumbai_g4058997";
    const kwSlug  = keyword.replace(/ /g, "-");
    const pageUrl = `https://www.olx.in/${locSlug}/q-${kwSlug}`;

    try {
      const resp = await fetch(pageUrl, {
        headers: {
          "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
          "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
          "Accept-Language": "en-IN,en;q=0.9",
          "Cache-Control":   "no-cache",
        },
      });

      const html = await resp.text();

      if (resp.status !== 200) {
        return json({ error: `OLX returned HTTP ${resp.status}`, pageUrl });
      }

      // Extract window.__APP = { ... }
      // Find start
      const startMarker = "window.__APP = ";
      const startIdx = html.indexOf(startMarker);
      if (startIdx === -1) {
        return json({
          error: "window.__APP not found",
          htmlLen: html.length,
          preview: html.slice(0, 300),
        }, 500);
      }

      // Count braces to find end of JSON object
      let raw = html.slice(startIdx + startMarker.length);
      let depth = 0, end = 0, inStr = false, escape = false;
      for (let i = 0; i < raw.length; i++) {
        const c = raw[i];
        if (escape) { escape = false; continue; }
        if (c === '\\' && inStr) { escape = true; continue; }
        if (c === '"') { inStr = !inStr; continue; }
        if (inStr) continue;
        if (c === '{') depth++;
        else if (c === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
      }

      const jsonStr = raw.slice(0, end);
      let appData;
      try {
        appData = JSON.parse(jsonStr);
      } catch(e) {
        return json({ error: "JSON parse failed: " + e.message, preview: jsonStr.slice(0, 300) }, 500);
      }

      // Dig into appData.props to find ads
      // OLX structure: appData.props.listingData.ads or similar
      const props = appData?.props || {};
      const ads = findDeep(props, "ads") || [];

      return json({
        ads,
        total: ads.length,
        props_keys: Object.keys(props).slice(0, 20),
      });

    } catch(err) {
      return json({ error: String(err), pageUrl }, 500);
    }
  }
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
  });
}

function findDeep(obj, key, depth = 0) {
  if (depth > 8 || obj === null || typeof obj !== "object") return null;
  if (key in obj && Array.isArray(obj[key]) && obj[key].length > 0) return obj[key];
  for (const k of Object.keys(obj)) {
    const r = findDeep(obj[k], key, depth + 1);
    if (r) return r;
  }
  return null;
}
