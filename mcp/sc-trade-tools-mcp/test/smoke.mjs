/**
 * End-to-end smoke test: spawn the built server over stdio, list tools, and call one
 * public tool (must return live data) plus one token-gated tool (must fail clearly).
 *
 * Run: node test/smoke.mjs   (requires `npm run build` first)
 */
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const CLI = resolve(__dirname, '..', 'dist', 'src', 'cli.js');

const EXPECTED = [
  'stt_list_commodities',
  'stt_list_locations',
  'stt_list_ships',
  'stt_get_commodity_transactions',
  'stt_get_crowdsource_listings',
  'stt_find_best_buyers',
  'stt_plan_itinerary',
  'stt_build_circuit',
  'stt_stock_range',
];

let failures = 0;
const check = (ok, label, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}${detail ? ` — ${detail}` : ''}`);
  if (!ok) failures++;
};

const transport = new StdioClientTransport({ command: 'node', args: [CLI] });
const client = new Client({ name: 'smoke', version: '1.0.0' });

try {
  await client.connect(transport);

  const { tools } = await client.listTools();
  const names = tools.map((t) => t.name);
  for (const n of EXPECTED) check(names.includes(n), `tool registered: ${n}`);

  const commodities = await client.callTool({ name: 'stt_list_commodities', arguments: {} });
  const text = commodities?.content?.[0]?.text ?? '';
  let parsed = [];
  try {
    parsed = JSON.parse(text);
  } catch {}
  check(Array.isArray(parsed) && parsed.length > 0, 'stt_list_commodities returns live data', `${parsed.length} items`);

  const ships = await client.callTool({ name: 'stt_list_ships', arguments: {} });
  const shipsText = ships?.content?.[0]?.text ?? '';
  check(shipsText.includes('maxBoxSizeInScu'), 'stt_list_ships includes maxBoxSizeInScu');

  const gated = await client.callTool({
    name: 'stt_find_best_buyers',
    arguments: { commodityName: 'Taranite', commodityQuantityInScu: 64 },
  });
  const gatedText = gated?.content?.[0]?.text ?? '';
  const tokenSet = Boolean(process.env.SC_TRADE_TOOLS_TOKEN);
  if (tokenSet) {
    check(!gatedText.includes('SC_TRADE_TOOLS_TOKEN is not set'), 'stt_find_best_buyers runs with token');
  } else {
    check(gatedText.includes('SC_TRADE_TOOLS_TOKEN is not set'), 'stt_find_best_buyers explains missing token');
  }

  // 货源范围：用 sc-trade.tools 众包的多批次上报看稳定性
  const range = await client.callTool({
    name: 'stt_stock_range',
    arguments: { commodity: 'titanium', location: 'baijini', maxPages: 6 },
  });
  const rangeText = range?.content?.[0]?.text ?? '';
  let rj = {};
  try { rj = JSON.parse(rangeText); } catch {}
  check(Boolean(rj.found), 'stt_stock_range finds submissions', `${rj.submissions ?? 0} 条, trend=${rj.trend ?? '?'}`);

  await client.close();
} catch (err) {
  check(false, 'transport/handshake', String(err).slice(0, 200));
  process.exitCode = 1;
}

console.log(failures === 0 ? '\nsmoke OK' : `\nsmoke FAILURES: ${failures}`);
process.exit(failures === 0 ? 0 : 1);
