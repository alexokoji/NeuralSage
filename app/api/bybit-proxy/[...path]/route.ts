/**
 * Bybit Testnet Proxy — runs as a Vercel Serverless Function in us-east-1
 * so Bybit's CloudFront geo-restriction is bypassed (US IP guaranteed).
 *
 * Set in Render environment:
 *   BYBIT_TESTNET_PROXY_URL = https://<your-vercel-domain>/api/bybit-proxy
 *   BYBIT_PROXY_SECRET      = neuraltrade-proxy-secret
 *
 * Set in Vercel environment (project settings → Environment Variables):
 *   BYBIT_PROXY_SECRET = neuraltrade-proxy-secret
 */

export const runtime = 'nodejs';
// Force this function to run in us-east-1 (Vercel default serverless region)
export const preferredRegion = 'iad1';

const BYBIT_BASE = 'https://api-testnet.bybit.com';
const SECRET = process.env.BYBIT_PROXY_SECRET ?? '';

export async function GET(req: Request, { params }: { params: { path: string[] } }) {
  return proxy(req, params.path);
}

export async function POST(req: Request, { params }: { params: { path: string[] } }) {
  return proxy(req, params.path);
}

async function proxy(req: Request, pathSegments: string[]): Promise<Response> {
  // Auth gate
  if (SECRET && req.headers.get('X-Proxy-Secret') !== SECRET) {
    return new Response('Forbidden', { status: 403 });
  }

  const { search } = new URL(req.url);
  const upstream = `${BYBIT_BASE}/${pathSegments.join('/')}${search}`;

  // Forward only Bybit signing headers
  const forward = new Headers();
  for (const h of ['X-BAPI-API-KEY', 'X-BAPI-SIGN', 'X-BAPI-SIGN-TYPE',
                    'X-BAPI-TIMESTAMP', 'X-BAPI-RECV-WINDOW', 'Content-Type']) {
    const v = req.headers.get(h);
    if (v) forward.set(h, v);
  }

  const body = req.method === 'GET' ? undefined : await req.arrayBuffer();

  const resp = await fetch(upstream, {
    method: req.method,
    headers: forward,
    body: body ? Buffer.from(body) : undefined,
  });

  const data = await resp.arrayBuffer();
  return new Response(data, {
    status: resp.status,
    headers: { 'Content-Type': 'application/json' },
  });
}
