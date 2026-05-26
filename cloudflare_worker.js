/**
 * OLX Proxy Worker — with real browser cookies (Akamai bypass)
 *
 * IMPORTANT: These cookies expire in ~24 hours.
 * When scraper stops working again, repeat the cookie refresh:
 *   1. Open olx.in/mumbai_g4058997/q-mobile in Chrome
 *   2. DevTools Console → type:  copy(document.cookie)  → Enter
 *      (if that gives undefined, use Application > Cookies tab instead)
 *   3. Paste new cookies into this worker → redeploy
 *
 * HOW TO AUTOMATE REFRESH (optional):
 *   Add OLX_COOKIE as a GitHub Secret, update worker to read it via env var.
 */

// ─── Paste fresh cookies here when they expire ───────────────
const OLX_COOKIE = `WZRK_G=1371028fa2c143a9994b6c2b81bd2fc5; _clck=12jqp89%5E2%5Eg6d%5E0%5E2337; showLocationToolTip=2026-05-26T00:01:44.347Z; _fbp=fb.1.1779753706613.381239966773703711; _ga=GA1.1.GA1.2.6771395414.1779753706097; _pubcid=da89c344-2cdc-4bb8-8f71-c6ab5790f5cc; _cc_id=c0b056a204d3396579ebba356b7a2061; panoramaId_expiry=1780358507662; panoramaId=ff3ae2386d8715cc34886f1db107185ca02c75796d95faa09715c937a65c8ae3; panoramaIdType=panoDevice; locationPath=%5B%7B%22id%22%3A4058997%2C%22name%22%3A%22Mumbai%22%2C%22type%22%3A%22CITY%22%2C%22longitude%22%3A72.8605%2C%22latitude%22%3A19.0591%2C%22parentId%22%3A2001163%7D%5D; pbjs_debug=0; si=1779770034750-6431033d-4ef6-4547-90d8-1b9bce982cfd; relevanceUser=05481058264880823; bm_sz=A4383EF53F77D16364152FCDF9DB4FF4~YAAQ7P3UF8ISa0yeAQAAywOTYh8QfAf7OWThUXHVDpyQXl0kdXTyaOLiVoykz/MCsa/OWI2fkG9buiq2t47Kqw0/fwLRMG4aWoflr2b+ygvC9TdUPka2voFwsuYlBo5QrJKsXGx7K4WU7llF8JQkHFE3flgEyp6orgppHaLmHryD7DpQEQGQTnS6rRykgOxJdBMXyd3e2k4h6UmzZlMPFYV/UbZYtWeYl7VOBziCp8PfQCh16LWSGsPhWGmD9CGm5jXEwdJ3hJMdUm3Qb4X1tTLrtxiu/2hXLqbcH/r5oQJRzFVfyB0gGWjFkM8cFwqyduxzHr4OC7dsqd9Uf1rR8T9rNHPcuST6xsnUMcBPzHTRoRSAgA==~3621702~3289156; _abck=D827C4B0EC3F4D4E221712DA0371F5DB~0~YAAQ7P3UF/0Sa0yeAQAAIQqTYg9ZAi+faOE3SZP2ayAq0Bd776A0BPkigoBizHUT70uXVW4L8o6D2WR7C/M21P6DZBm0LgRokS2CUPrXyzdPieQ/YkPf2XKhOVHrRzXsLIeFAIfjqCLMkEpReNxRgk1/NcKzUoIS8OeIRxxwTJH2tXNDPphHK9fqf+s0HCx1xjOLQWNpArrDv7eK6+hMNSGU+MQBHNBG/o+6v2TUzUbIta1f8wckQlPvVJLdQIijgAWKXC4pYOjrLfHCGUiJgMaADO/uKxzpImXq6TzFdOuioCIZ+4ayXW/MJJssj03x7ZDqReWrS5pwhXu3qVPhufw4bg7N88WIzcA8VfQnYXTPWq/jWe8+dKIf4aAUdYDJA8yoMGU0P/T91OlowBKh8nI5Ut2x6HMWZBu4wKW5CA3hAUSRu5xolN4+IkgDj7E/LhV4bp92w2kzZ88ehAOopQSn9RN4jDRuKko+RkTYr8L6P6ODERYxJaVe5xWBQXHkYgItsrqy49JxeCBQiQ2eojGMvPC7Oabq8wdFaIrwI26bmYytistGi0BwCPHvlPhl+5bB4w7jWQhX0HIqwEDgab56joxC4MkX+lWuDLOHXgVBS/SR1214Gl+9H9kyNwInUExixMCDUPACYZMWWOGGYoBwl3yJyDg+j4x4TZwPdoo3YBqmhdPgXwqo3pwkpziJyMRWPwFZ19B92IhKtnS/XTiJ~-1~-1~-1~AAQAAAAF%2f%2f%2f%2f%2fwa1EksWBJYsojZbVT28lL4dwvi5k2BWh5lt9DR16QnlFa%2fVxCVhZCcEll%2fAp9gc86VJp%2f30E6fB5mnK8aEl0nMxj8TDDIXavliny+rtW7zPoOMrjDlCK7eMXHLqxRjafmeLMY0XPmTxcK8HgCcRCIBYnxWX+i%2fjkCo6VPFxOQ%3d%3d~-1; ak_bmsc=619C98A6CBB7D514DE32743E9EFE3BDD~000000000000000000000000000000~YAAQ7P3UFyQTa0yeAQAAHBGTYh+qsZ1U9u3AEcgTWArPNNrohfECCivuMz2PU/iNcvroCWzZdx38CJE6aMe+5e3cMvZST11oe3EjK0zXm9c2kve8lwgqfEEU1JB4dpYETGb912rVeCpyFa385j2QAeA3I5fu+BPrz1vflTnzpIa7SCPoH1OZEu7Xboek5RqhmHlXgy38g2N2Swn+YdnCEe6O2MKC9EeMaNnvxH28Ft/H/9dz/Gfhd/Hdc9AxWdRNEfZNhcEK52SZV3Toh+cq/J7IfaDjuz28L0RDuKknO6harpt5n7loZvNHOfw0DUMTtoQTUbpKNVGQ0e+oPNv8jXTQxHPCOUuso9Pg/M3H28xs5Iil0XOf19p6DxVFv4VRacLLymqBbVixpR85dsW9NRWITIDBijcfQFwO1dc2weaFD7+D4f4xfA0IinEdokvilzX2WBcRMjZQut/yEUfi684=; bm_sv=3461D4D73D9707C3B2F714F0786B3BDA~YAAQnf3UF6NAeECeAQAAIGOZYh/2yib7Rm+nSXPzjm2JZhblIiZ4Q8IH2mHcTJsc0Lzvl5RNK+3cDFIlqwz+pRA0uRNIjqF1EThwhAE/tS49nBHWFxhf3w3vSE6/+56cskLGwCU8638tVr8+C9hoHvGwwyZV8YkHTAgMGfXigdY2rDJqGr8+Dp7vuIh1r+PRgI6OZZMncTF0I3SiaV7g/BajnrezIbomOL+dg7AIE91e9/jtVXhbyt9aKl//TtjxIw==~1; _ga_KSZERL1094=GS2.1.s1779770262$o2$g1$t1779770740$j35$l0$h0; WZRK_S_848-646-995Z=%7B%22p%22%3A2%2C%22s%22%3A1779770263%2C%22t%22%3A1779770740%7D`;
// ─────────────────────────────────────────────────────────────

export default {
  async fetch(request) {
    const incomingUrl = new URL(request.url);
    const targetUrl   = incomingUrl.searchParams.get("url");

    if (!targetUrl || !targetUrl.startsWith("https://www.olx.in/")) {
      return new Response(JSON.stringify({ error: "Only olx.in URLs allowed" }), {
        status: 400, headers: { "Content-Type": "application/json" },
      });
    }

    try {
      const olxResp = await fetch(targetUrl, {
        method:   "GET",
        redirect: "follow",
        headers: {
          "User-Agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
          "Accept":             "application/json, text/plain, */*",
          "Accept-Language":    "en-IN,en-GB;q=0.9,en;q=0.8,hi;q=0.7",
          "Accept-Encoding":    "gzip, deflate, br",
          "Referer":            "https://www.olx.in/mumbai_g4058997/q-mobile",
          "Origin":             "https://www.olx.in",
          "x-panamera-id":      "web_in",
          "DNT":                "1",
          "Sec-Fetch-Dest":     "empty",
          "Sec-Fetch-Mode":     "cors",
          "Sec-Fetch-Site":     "same-origin",
          "sec-ch-ua":          '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
          "sec-ch-ua-mobile":   "?0",
          "sec-ch-ua-platform": '"Windows"',
          "Cookie":             OLX_COOKIE,
        },
      });

      const body   = await olxResp.text();
      const status = olxResp.status;

      return new Response(body, {
        status,
        headers: {
          "Content-Type":                "application/json",
          "Access-Control-Allow-Origin": "*",
          "X-OLX-Status":                String(status),
        },
      });

    } catch (err) {
      return new Response(JSON.stringify({ error: String(err) }), {
        status: 500, headers: { "Content-Type": "application/json" },
      });
    }
  },
};
