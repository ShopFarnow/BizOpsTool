/**
 * OLX Proxy Worker — deploy this ONCE on Cloudflare (free forever)
 * 
 * HOW TO DEPLOY (5 minutes, free):
 *   1. Go to https://workers.cloudflare.com → Sign up (free)
 *   2. Click "Create Worker"
 *   3. Paste this entire file → click "Save and Deploy"
 *   4. Copy your worker URL: https://olx-proxy.<your-subdomain>.workers.dev
 *   5. Add it as GitHub secret: WORKER_URL = https://olx-proxy.<your-subdomain>.workers.dev
 *
 * WHY THIS WORKS:
 *   - Cloudflare's IPs are trusted CDN IPs — OLX does NOT block them
 *   - GitHub Actions IPs (Azure) are datacenter IPs — OLX DOES block them
 *   - This worker acts as a 1-hop relay: GH Actions → Cloudflare → OLX
 *   - 100,000 free requests/day — you'll use ~8-16 per run
 *   - No credit card required for Cloudflare free tier
 */

export default {
  async fetch(request) {
    // Simple security: require a shared secret header
    const secret = request.headers.get("x-worker-secret");
    const expected = globalThis.WORKER_SECRET || ""; // set as env var in Cloudflare dashboard (optional)

    if (expected && secret !== expected) {
      return new Response("Unauthorized", { status: 401 });
    }

    const incomingUrl = new URL(request.url);
    const targetUrl = incomingUrl.searchParams.get("url");

    if (!targetUrl || !targetUrl.startsWith("https://www.olx.in/")) {
      return new Response(JSON.stringify({ error: "Only olx.in URLs allowed" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    try {
      const olxResp = await fetch(targetUrl, {
        headers: {
          "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
          "Accept":          "application/json, text/plain, */*",
          "Accept-Language": "en-IN,en;q=0.9",
          "Accept-Encoding": "gzip, deflate, br",
          "Referer":         "https://www.olx.in/",
          "Origin":          "https://www.olx.in",
          "x-panamera-id":   "web_in",
          "DNT":             "1",
          "Sec-Fetch-Dest":  "empty",
          "Sec-Fetch-Mode":  "cors",
          "Sec-Fetch-Site":  "same-origin",
        },
        // Cloudflare Workers follow redirects automatically
      });

      const body = await olxResp.text();

      return new Response(body, {
        status: olxResp.status,
        headers: {
          "Content-Type":                "application/json",
          "Access-Control-Allow-Origin": "*",
          "X-OLX-Status":                String(olxResp.status),
        },
      });
    } catch (err) {
      return new Response(JSON.stringify({ error: String(err) }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }
  },
};
