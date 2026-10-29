import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { registerReferenceTools } from './tools/reference.js';
import { registerPlanningTools } from './tools/planning.js';
import { hasToken } from './client.js';

export function createServer(): McpServer {
  const server = new McpServer(
    { name: 'sc-trade-tools-mcp', version: '0.1.0' },
    {
      instructions: [
        'Read-only tools (stt_list_*, stt_get_*) work without any credential.',
        hasToken()
          ? 'Planning tools are enabled (SC_TRADE_TOOLS_TOKEN is set).'
          : 'Planning tools (stt_find_best_buyers / stt_plan_itinerary / stt_build_circuit) need ' +
            'SC_TRADE_TOOLS_TOKEN, which SC Trade Tools grants to Patreon supporters.',
        '',
        'Data provenance: SC Trade Tools prices are player crowd-sourced — a DIFFERENT dataset from UEX. ' +
        'In this repo the agreed authority order is: player observation (screenshot/verbal) > UEX > sc-trade.tools. ' +
        'Treat sc-trade.tools as a cross-check on UEX, not a replacement, and never let it override something the user actually saw in game.',
      ].join('\n'),
    },
  );

  registerReferenceTools(server);
  registerPlanningTools(server);
  return server;
}
