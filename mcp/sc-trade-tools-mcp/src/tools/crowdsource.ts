/**
 * Crowd-sourced stock tooling.
 *
 * `GET /api/crowdsource/commodity-listings` is the one public endpoint that carries
 * per-location SUPPLY information: `quantity`, `saturation` (0..1) and a `timestamp`.
 * UEX only gives a discrete 1-7 status, so this is the incremental value of
 * sc-trade.tools: multiple community submissions over time let you see whether a
 * supply is stable, refilling, or draining.
 */
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';
import { apiGet, ApiError } from '../client.js';

interface Listing {
  location?: string;
  transaction?: string;
  commodity?: string;
  price?: number;
  quantity?: number;
  saturation?: number;
  boxSizesInScu?: unknown;
  batchId?: string;
  timestamp?: string;
}

function json(data: unknown) {
  return { content: [{ type: 'text' as const, text: JSON.stringify(data, null, 2) }] };
}

function fail(err: unknown) {
  const msg = err instanceof ApiError ? err.message : `request failed: ${String(err)}`;
  return { content: [{ type: 'text' as const, text: msg }], isError: true };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export function registerCrowdsourceTools(server: McpServer): void {
  server.tool(
    'stt_stock_range',
    'Supply range/trend for a commodity at a location, from crowd-sourced submissions. ' +
      'Returns the observed quantity range, saturation range, first/last submission time and ' +
      'whether supply looks stable, refilling or draining. This is what UEX cannot tell you ' +
      '(UEX only exposes a 1-7 status). No token required.',
    {
      commodity: z.string().describe('Commodity name as used by sc-trade.tools (lowercase, e.g. "titanium")'),
      location: z.string().optional().describe('Optional location substring, e.g. "baijini" or "seraphim"'),
      maxPages: z.number().int().min(1).max(40).optional().describe('Pages to scan (100 listings each, default 8)'),
    },
    async ({ commodity, location, maxPages }) => {
      try {
        const pages = maxPages ?? 8;
        const matches: Listing[] = [];
        const com = commodity.trim().toLowerCase();
        const loc = (location || '').trim().toLowerCase();

        for (let p = 0; p < pages; p++) {
          const data = (await apiGet('/api/crowdsource/commodity-listings', { page: p })) as {
            content?: Listing[];
          };
          const rows = data?.content ?? [];
          if (!rows.length) break;
          for (const r of rows) {
            if ((r.commodity || '').toLowerCase() !== com) continue;
            if (loc && !(r.location || '').toLowerCase().includes(loc)) continue;
            matches.push(r);
          }
          if (p < pages - 1) await sleep(250); // be polite to the upstream
        }

        if (!matches.length) {
          return json({
            commodity,
            location: location ?? null,
            found: false,
            note: 'no crowd-sourced submission matched (try a shorter location substring, or raise maxPages)',
          });
        }

        matches.sort((a, b) => (a.timestamp || '').localeCompare(b.timestamp || ''));
        const qs = matches.map((r) => r.quantity ?? 0);
        const ss = matches.map((r) => r.saturation ?? 0);
        const first = qs[0];
        const last = qs[qs.length - 1];
        const drift = last - first;
        const rel = first ? drift / first : 0;
        let trend: string;
        if (qs.length < 2) trend = 'single submission — no trend';
        else if (Math.abs(rel) < 0.1) trend = 'stable';
        else if (rel > 0) trend = 'refilling';
        else trend = 'draining';

        return json({
          commodity,
          location: location ?? null,
          found: true,
          submissions: matches.length,
          quantity: { min: Math.min(...qs), max: Math.max(...qs), first, last },
          saturation: { min: Math.min(...ss), max: Math.max(...ss) },
          window: { from: matches[0].timestamp, to: matches[matches.length - 1].timestamp },
          trend,
          caution: 'Community-reported and not real-time; upstream states the site cannot know live stock.',
          series: matches.map((r) => ({
            at: r.timestamp,
            location: r.location,
            transaction: r.transaction,
            price: r.price,
            quantity: r.quantity,
            saturation: r.saturation,
          })),
        });
      } catch (e) {
        return fail(e);
      }
    },
  );
}
