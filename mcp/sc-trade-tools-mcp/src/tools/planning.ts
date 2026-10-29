/**
 * Planning tools — POST /api/tools/{buyers,circuits/{id},itinerary}.
 *
 * These three endpoints declare a REQUIRED `token` header in the OpenAPI spec,
 * so they are only usable when SC_TRADE_TOOLS_TOKEN is set (granted to Patreon
 * supporters). Missing-field defaults below follow the spec's required fields;
 * callers can override any of them.
 */
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';
import { apiPost, ApiError, requireToken, hasToken } from '../client.js';

const enc = encodeURIComponent;

function json(data: unknown) {
  return { content: [{ type: 'text' as const, text: JSON.stringify(data, null, 2) }] };
}

function fail(err: unknown) {
  const msg = err instanceof Error ? err.message : `request failed: ${String(err)}`;
  return { content: [{ type: 'text' as const, text: msg }], isError: true };
}

/** Shared filter fields; `blacklist` + empty list = "no restriction". */
const locationNames = z.array(z.string()).optional();
const whitelistFlag = z.enum(['blacklist', 'whitelist']).optional();

function commonFilter(extra: Record<string, z.ZodTypeAny> = {}) {
  return {
    locationNames,
    locationNamesType: whitelistFlag,
    locationTypes: z.array(z.string()).optional(),
    locationTypesType: whitelistFlag,
    factionNames: z.array(z.string()).optional(),
    factionsNamesType: whitelistFlag,
    minSecurityLevel: z.number().int().min(0).max(99).optional().describe('0 = no restriction (default 0)'),
    supportedBoxSizeInScu: z.number().int().min(1).max(32).optional().describe('Max box size the ship can load (default 32)'),
    avoidHiddenLocations: z.boolean().optional().describe('Exclude locations not on the in-game map (default false)'),
    allowWaitTimes: z.boolean().optional().describe('Allow stops requiring an inventory wait (default true)'),
    minInventorySizeInScu: z.number().int().min(0).optional().describe('Minimum buyer inventory in SCU (default 0)'),
    ...extra,
  };
}

type Args = Record<string, unknown>;

function buildFilters(args: Args, token: string) {
  return {
    locationNames: args.locationNames ?? [],
    locationNamesType: args.locationNamesType ?? 'blacklist',
    locationTypes: args.locationTypes ?? [],
    locationTypesType: args.locationTypesType ?? 'blacklist',
    factionNames: args.factionNames ?? [],
    factionsNamesType: args.factionsNamesType ?? 'blacklist',
    minSecurityLevel: args.minSecurityLevel ?? 0,
    supportedBoxSizeInScu: args.supportedBoxSizeInScu ?? 32,
    avoidHiddenLocations: args.avoidHiddenLocations ?? false,
    allowWaitTimes: args.allowWaitTimes ?? true,
    minInventorySizeInScu: args.minInventorySizeInScu ?? 0,
    token,
  };
}

const NEEDS_TOKEN = hasToken()
  ? ''
  : ' [UNAVAILABLE: SC_TRADE_TOOLS_TOKEN not set]';

export function registerPlanningTools(server: McpServer): void {
  server.tool(
    'stt_find_best_buyers',
    `Best places to sell a commodity you are carrying (POST /api/tools/buyers). Requires an API token.${NEEDS_TOKEN}`,
    commonFilter({
      commodityName: z.string().describe('Commodity to sell, e.g. "Taranite"'),
      commodityQuantityInScu: z.number().int().min(1).describe('Quantity in SCU'),
      isCargoStolen: z.boolean().optional().describe('Stolen cargo may only be sold at certain places (default false)'),
    }),
    async (raw) => {
      const args = raw as Args;
      try {
        const token = requireToken();
        const body = {
          ...buildFilters(args, token),
          commodityName: args.commodityName,
          commodityQuantityInScu: args.commodityQuantityInScu,
          isCargoStolen: args.isCargoStolen ?? false,
        };
        return json(await apiPost('/api/tools/buyers', body, token));
      } catch (e) {
        return fail(e);
      }
    },
  );

  server.tool(
    'stt_plan_itinerary',
    `Plan a multi-stop trade itinerary for a ship and budget (POST /api/tools/itinerary). Requires an API token.${NEEDS_TOKEN}`,
    commonFilter({
      commodityNames: z.array(z.string()).optional(),
      commodityNamesType: whitelistFlag,
      commodityTypes: z.array(z.string()).optional(),
      commodityTypesType: whitelistFlag,
      investment: z.number().int().min(1).describe('aUEC to invest'),
      profitType: z.enum(['time', 'pure']).optional().describe("'time' = profit per second, 'pure' = max profit (default 'pure')"),
      ship: z.string().describe('Ship name — see stt_list_ships'),
      maxStops: z.number().int().min(1).max(5).optional().describe('Max trade stops, 1-5 (default 3)'),
      useAutoLoading: z.boolean().optional().describe('Account for auto-loading time/cost (default false)'),
      smartFilters: z.boolean().optional().describe('Auto-exclude low-inventory listings (default true)'),
    }),
    async (raw) => {
      const args = raw as Args;
      try {
        const token = requireToken();
        const body = {
          ...buildFilters(args, token),
          commodityNames: args.commodityNames ?? [],
          commodityNamesType: args.commodityNamesType ?? 'blacklist',
          commodityTypes: args.commodityTypes ?? [],
          commodityTypesType: args.commodityTypesType ?? 'blacklist',
          maxVolume: 0, // deprecated field, still required
          investment: args.investment,
          profitType: args.profitType ?? 'pure',
          ship: args.ship,
          maxStops: args.maxStops ?? 3,
          useAutoLoading: args.useAutoLoading ?? false,
          smartFilters: args.smartFilters ?? true,
        };
        return json(await apiPost('/api/tools/itinerary', body, token));
      } catch (e) {
        return fail(e);
      }
    },
  );

  server.tool(
    'stt_build_circuit',
    `Build a circular (closed-loop) trade route around an existing trade (POST /api/tools/circuits/{tradeId}). Requires an API token.${NEEDS_TOKEN}`,
    {
      tradeId: z.string().describe('Trade id to build the circuit from (as returned by the itinerary/trade endpoints)'),
      ...commonFilter({
        commodityNames: z.array(z.string()).optional(),
        commodityNamesType: whitelistFlag,
        commodityTypes: z.array(z.string()).optional(),
        commodityTypesType: whitelistFlag,
        investment: z.number().int().min(1).optional().describe('aUEC to invest'),
        profitType: z.enum(['time', 'pure']).optional(),
        ship: z.string().optional().describe('Ship name — see stt_list_ships'),
        maxStops: z.number().int().min(1).max(5).optional(),
        useAutoLoading: z.boolean().optional(),
        smartFilters: z.boolean().optional(),
      }),
    },
    async (raw) => {
      const args = raw as Args;
      try {
        const token = requireToken();
        const body = {
          ...buildFilters(args, token),
          commodityNames: args.commodityNames ?? [],
          commodityNamesType: args.commodityNamesType ?? 'blacklist',
          commodityTypes: args.commodityTypes ?? [],
          commodityTypesType: args.commodityTypesType ?? 'blacklist',
          maxVolume: 0,
          investment: args.investment,
          profitType: args.profitType ?? 'pure',
          ship: args.ship,
          maxStops: args.maxStops ?? 3,
          useAutoLoading: args.useAutoLoading ?? false,
          smartFilters: args.smartFilters ?? true,
        };
        return json(await apiPost(`/api/tools/circuits/${enc(String(args.tradeId))}`, body, token));
      } catch (e) {
        return fail(e);
      }
    },
  );
}
