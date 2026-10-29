/**
 * HTTP client for the public SC Trade Tools API.
 *
 * Base: https://sc-trade.tools  ·  OpenAPI: https://sc-trade.tools/v3/api-docs
 * (Swagger UI: https://sc-trade.tools/swagger-ui/index.html)
 *
 * Auth model, verified 2026-10-29 against the OpenAPI spec:
 *   - Read-only endpoints (commodities / locations / ships / crowdsource listings…)
 *     need NO token — confirmed with live requests.
 *   - The three planning endpoints (POST /api/tools/buyers, /api/tools/circuits/{id},
 *     /api/tools/itinerary) declare a REQUIRED `token` header. Tokens are granted to
 *     Patreon supporters of the project.
 *   - The project also exposes a remote MCP server at https://sc-trade.tools/mcp
 *     (HTTP, stateless, Bearer token). This local server talks to the REST API instead,
 *     so it degrades gracefully to the public endpoints when no token is configured.
 */
const BASE_URL = (process.env.SC_TRADE_TOOLS_BASE_URL || 'https://sc-trade.tools').replace(/\/+$/, '');
const TIMEOUT_MS = Number(process.env.SC_TRADE_TOOLS_TIMEOUT_MS || 30_000);
export class ApiError extends Error {
    status;
    constructor(status, message) {
        super(message);
        this.name = 'ApiError';
        this.status = status;
    }
}
export function hasToken() {
    return Boolean(process.env.SC_TRADE_TOOLS_TOKEN);
}
/** Returns the configured token or throws a message that tells the user how to get one. */
export function requireToken() {
    const token = process.env.SC_TRADE_TOOLS_TOKEN;
    if (!token) {
        throw new Error('SC_TRADE_TOOLS_TOKEN is not set. This endpoint requires an API token, which ' +
            'SC Trade Tools grants to its Patreon supporters (https://www.patreon.com/cw/sc_trade_tools/membership). ' +
            'Set the env var to enable planning tools; the read-only tools work without it.');
    }
    return token;
}
function headers(token) {
    const h = { Accept: 'application/json' };
    if (token)
        h.Authorization = `Bearer ${token}`;
    return h;
}
async function request(path, init) {
    const url = `${BASE_URL}${path}`;
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
    try {
        const res = await fetch(url, { ...init, signal: ctrl.signal });
        if (res.status === 401 || res.status === 403) {
            throw new ApiError(res.status, `${res.status} — API token required (or lacks access) for ${path}`);
        }
        if (res.status === 429) {
            throw new ApiError(429, '429 — rate limited by SC Trade Tools. Back off and retry later.');
        }
        const text = await res.text();
        if (!res.ok) {
            throw new ApiError(res.status, `${res.status} for ${path}: ${text.slice(0, 300)}`);
        }
        if (!text)
            return null;
        try {
            return JSON.parse(text);
        }
        catch {
            return text;
        }
    }
    catch (err) {
        if (err instanceof ApiError)
            throw err;
        if (err instanceof Error && err.name === 'AbortError') {
            throw new ApiError(504, `timeout after ${TIMEOUT_MS}ms calling ${path}`);
        }
        throw new ApiError(0, `request failed for ${path}: ${String(err).slice(0, 300)}`);
    }
    finally {
        clearTimeout(timer);
    }
}
export function apiGet(path, params) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params || {})) {
        if (v !== undefined && v !== '')
            qs.set(k, String(v));
    }
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    return request(`${path}${suffix}`, { method: 'GET', headers: headers() });
}
export function apiPost(path, body, token) {
    return request(path, {
        method: 'POST',
        headers: { ...headers(token), 'Content-Type': 'application/json' },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
}
export const BASE = BASE_URL;
//# sourceMappingURL=client.js.map