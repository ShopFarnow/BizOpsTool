/**
 * OLX Proxy Worker — paste this into Cloudflare Workers and deploy
 * 
 * FIX: Now returns the raw response body even if not JSON,
 * so the Python script can see exactly what OLX is saying back.
 * Also adds cookie consent bypass and updated headers.
 */

export default {
  async fetch(request) {
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
        method: "GET",
        redirect: "follow",
        headers: {
          "User-Agent":        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
          "Accept":            "application/json, text/plain, */*",
          "Accept-Language":   "en-IN,en-GB;q=0.9,en;q=0.8",
          "Accept-Encoding":   "gzip, deflate, br",
          "Referer":           "https://www.olx.in/",
          "Origin":            "https://www.olx.in",
          "x-panamera-id":     "web_in",
          "x-location-id":     "4058833",
          "DNT":               "1",
          "Sec-Fetch-Dest":    "empty",
          "Sec-Fetch-Mode":    "cors",
          "Sec-Fetch-Site":    "same-origin",
          "sec-ch-ua":         '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
          "sec-ch-ua-mobile":  "?0",
          "sec-ch-ua-platform": '"Windows"',
          // Cookie consent bypass — tells OLX we already accepted cookies
          "Cookie": "datadome=; optimizelyEndUserId=; __gads=; consent=true; _gcl_au=; _ga=;",
        },
      });

      const body = await olxResp.text();
      const status = olxResp.status;

      // Return whatever OLX sent back — let Python decide what to do with it
      return new Response(body, {
        status: status,
        headers: {
          "Content-Type":                olxResp.headers.get("content-type") || "application/json",
          "Access-Control-Allow-Origin": "*",
          "X-OLX-Status":                String(status),
          "X-OLX-URL":                   targetUrl,
        },
      });

    } catch (err) {
      return new Response(JSON.stringify({ error: String(err), url: targetUrl }), {
        status: 500,
        headers: { "Content-Type": "application/json" },
      });
    }
  },
};
