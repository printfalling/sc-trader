/**
 * Read-only tools — these hit public endpoints and need no API token.
 * Verified live 2026-10-29: /api/commodity/items, /api/locations, /api/ships,
 * /api/crowdsource/commodity-listings all return 200 unauthenticated.
 */
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';
import { apiGet, ApiError } from '../client.js';

function json(data: unknown) {
  return { content: [{ type: 'text' as const, text: JSON.stringify(data, null, 2) }] };
}

function fail(err: unknown) {
  const msg = err instanceof ApiError ? err.message : `request failed: ${String(err)}`;
  return { content: [{ type: 'text' as const, text: msg }], isError: true };
}

const enc = encodeURIComponent;

export function registerReferenceTools(server: McpServer): void {
  server.tool(
    'stt_list_commodities',
    'List all commodity names known to SC Trade Tools (GET /api/commodity/items). No token required.',
    {},
    async () => {
      try {
        return json(await apiGet('/api/commodity/items'));
      } catch (e) {
        return fail(e);
      }
    },
  );

  server.tool(
    'stt_list_locations',
    'List all known locations (systems, planets, moons, stations, outposts) with their type (GET /api/locations). No token required.',
    {},
    async () => {
      try {
        return json(await apiGet('/api/locations'));
      } catch (e) {
        return fail(e);
      }
    },
  );

  server.tool(
    'stt_list_ships',
    'List ships with the max box size each can load in SCU (GET /api/ships). Useful to fill supportedBoxSizeInScu. No token required.',
    {},
    async () => {
      try {
        return json(await apiGet('/api/ships'));
      } catch (e) {
        return fail(e);
      }
    },
  );

  server.tool(
    'stt_list_security_levels',
    'List known security levels (GET /api/security-levels). No token required.',
    {},
    async () => {
      try {
        return json(await apiGet('/api/security-levels'));
      } catch (e) {
        return fail(e);
      }
    },
  );

  server.tool(
    'stt_list_factions',
    'List factions (GET /api/factions). No token required.',
    {},
    async () => {
      try {
        return json(await apiGet('/api/factions'));
      } catch (e) {
        return fail(e);
      }
    },
  );

  server.tool(
    'stt_get_commodity_transactions',
    'Buy/sell listings for one commodity across all shops (GET /api/commodity/items/{name}/transactions). No token required.',
    { name: z.string().describe('Commodity name, e.g. "Taranite" or "Agricium (Ore)"') },
    async ({ name }) => {
      try {
        return json(await apiGet(`/api/commodity/items/${enc(name)}/transactions`));
      } catch (e) {
        return fail(e);
      }
    },
  );

  server.tool(
    'stt_get_shop_transactions',
    'All commodity listings at one shop (GET /api/commodity/shops/{name}/transactions). No token required.',
    { name: z.string().describe('Shop / terminal name as listed by stt_list_locations or the website') },
    async ({ name }) => {
      try {
        return json(await apiGet(`/api/commodity/shops/${enc(name)}/transactions`));
      } catch (e) {
        return fail(e);
      }
    },
  );

  server.tool(
    'stt_get_crowdsource_listings',
    'Community-submitted price listings (GET /api/crowdsource/commodity-listings, paginated). ' +
      'This is SC Trade Tools\' own crowd-sourced dataset — distinct from UEX. No token required.',
    { page: z.number().int().min(0).optional().describe('Page index (default 0)') },
    async ({ page }) => {
      try {
        return json(await apiGet('/api/crowdsource/commodity-listings', { page }));
      } catch (e) {
        return fail(e);
      }
    },
  );

  server.tool(
    'stt_get_trade_alternatives',
    'Alternative commodities for a given trade edge (GET /api/graphs/shop/edges/{edgeId}/alternatives). No token required.',
    { edgeId: z.string().describe('Trade edge id as returned by the trade route endpoints') },
    async ({ edgeId }) => {
      try {
        return json(await apiGet(`/api/graphs/shop/edges/${enc(edgeId)}/alternatives`));
      } catch (e) {
        return fail(e);
      }
    },
  );
}
