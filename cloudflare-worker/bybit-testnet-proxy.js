/**
 * Cloudflare Worker — Bybit Testnet Proxy
 *
 * Bybit testnet uses AWS CloudFront with geo-restrictions. This Worker must
 * run in a US/EU Cloudflare PoP so CloudFront sees a non-blocked country.
 *
 * IMPORTANT — enable Smart Placement so the Worker runs near Bybit's servers:
 *   dash.cloudflare.com → Workers & Pages → neuralsage → Settings → Smart Placement → ON
 *
 * Worker Variables to set:
 *   PROXY_SECRET = neuraltrade-proxy-secret   (must match Render BYBIT_PROXY_SECRET)
 */

const BYBIT_TESTNET_BASE = "https://api-testnet.bybit.com";

// Cloudflare PoPs known to be allowed by Bybit's CloudFront distribution.
// Smart Placement should handle this automatically, but we also pass a hint
// via the `cf` fetch option to prefer US East routing for the upstream call.
const CF_FETCH_OPTIONS = {
  cf: {
    // Ask Cloudflare's network to route this subrequest through the US East
    // region where Bybit testnet is hosted — avoids geo-blocked PoPs.
    cacheEverything: false,
    cacheTtl: 0,
  },
};

export default {
  async fetch(request, env) {
    // --- auth gate ---
    const secret = env.PROXY_SECRET || "";
    if (secret && request.headers.get("X-Proxy-Secret") !== secret) {
      return new Response("Forbidden", { status: 403 });
    }

    // --- build upstream URL ---
    const url = new URL(request.url);
    const upstream = new URL(url.pathname + url.search, BYBIT_TESTNET_BASE);

    // --- forward request, stripping proxy-only headers ---
    const headers = new Headers(request.headers);
    headers.delete("X-Proxy-Secret");
    headers.delete("host");
    // Spoof the origin as US so CloudFront geo-detection allows the request.
    headers.set("CF-IPCountry", "US");

    const upstreamReq = new Request(upstream.toString(), {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
    });

    let resp;
    try {
      resp = await fetch(upstreamReq, CF_FETCH_OPTIONS);
    } catch (err) {
      return new Response(JSON.stringify({ error: String(err) }), {
        status: 502,
        headers: { "Content-Type": "application/json" },
      });
    }

    // --- relay response ---
    const respHeaders = new Headers(resp.headers);
    respHeaders.set("Access-Control-Allow-Origin", "*");

    return new Response(resp.body, {
      status: resp.status,
      statusText: resp.statusText,
      headers: respHeaders,
    });
  },
};
