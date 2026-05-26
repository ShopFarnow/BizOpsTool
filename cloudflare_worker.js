/**
 * OLX Proxy Worker — window.__APP parser
 *
 * OLX embeds ALL listing data inside window.__APP = {...} in the HTML.
 * We fetch the page HTML, extract __APP, parse the JSON, return ads.
 * No API keys, no cookies, no buildId needed. Works permanently.
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
          "Accept-Encoding": "gzip, deflate, br",
          "Cache-Control":   "no-cache",
        },
      });

      const html = await resp.text();

      // Extract window.__APP = { ... } — it ends at }; on its own line
      // The JSON is large so we grab everything between the braces
      const appMatch = html.match(/window\.__APP\s*=\s*(\{[\s\S]*?\});\s*\n/);
      if (!appMatch) {
        // Try broader match
        const appMatch2 = html.match(/window\.__APP\s*=\s*(\{[\s\S]+)/);
        if (!appMatch2) {
          return new Response(JSON.stringify({
            error: "window.__APP not found in HTML",
            htmlLen: html.length,
            preview: html.slice(0, 500),
          }), { status: 500, headers: { "Content-Type": "application/json" } });
        }

        // Find the JSON by counting braces
        let raw = appMatch2[1];
        let depth = 0, end = 0;
        for (let i = 0; i < raw.length; i++) {
          if (raw[i] === '{') depth++;
          else if (raw[i] === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
        }
        raw = raw.slice(0, end);

        try {
          const appData = JSON.parse(raw);
          return buildResponse(appData, pageUrl);
        } catch(e) {
          return new Response(JSON.stringify({
            error: "JSON parse failed: " + e.message,
            raw_preview: raw.slice(0, 500),
          }), { status: 500, headers: { "Content-Type": "application/json" } });
        }
      }

      const appData = JSON.parse(appMatch[1]);
      return buildResponse(appData, pageUrl);

    } catch (err) {
      return new Response(JSON.stringify({ error: String(err), pageUrl }), {
        status: 500, headers: { "Content-Type": "application/json" },
      });
    }
  },
};

function buildResponse(appData, pageUrl) {
  // Navigate the __APP structure to find ads
  // Structure: appData.props -> various paths
  const props = appData.props || appData;

  // Try multiple paths where OLX might store ads
  const ads =
    props?.listingData?.ads ||
    props?.data?.ads ||
    props?.listing?.ads ||
    props?.initialData?.ads ||
    props?.pageData?.ads ||
    findAds(props) ||
    [];

  return new Response(JSON.stringify({
    ads,
    total: ads.length,
    props_keys: Object.keys(props || {}).slice(0, 15),
    app_keys:   Object.keys(appData || {}).slice(0, 10),
  }), {
    status: 200,
    headers: {
      "Content-Type":                "application/json",
      "Access-Control-Allow-Origin": "*",
    },
  });
}

// Recursively search for an "ads" array in the object tree
function findAds(obj, depth = 0) {
  if (depth > 6 || !obj || typeof obj !== "object") return null;
  if (Array.isArray(obj?.ads) && obj.ads.length > 0) return obj.ads;
  for (const key of Object.keys(obj)) {
    const result = findAds(obj[key], depth + 1);
    if (result) return result;
  }
  return null;
}
