# sc-trade-tools-mcp

MCP server that exposes the public [SC Trade Tools](https://sc-trade.tools) REST API.

This was built by **checking the upstream OpenAPI spec ourselves** rather than copying the
third-party `CayrisTech/sc-trade-tools-mcp` — that implementation never calls the API at all,
it scrapes the `https://sc-trade.tools/best-buyer?q=…` web page.

## Facts verified 2026-10-29 against `https://sc-trade.tools/v3/api-docs`

| | |
|---|---|
| Spec | OpenAPI 3.0.1, 34 endpoints, `SC Trade Tools API` |
| Swagger UI | https://sc-trade.tools/swagger-ui/index.html |
| Read-only endpoints | **mostly no token** — verified live 200s on `/api/commodity/items`, `/api/commodity/shops`, `/api/locations`, `/api/ships`, `/api/factions`, `/api/security-levels`, `/api/location-types`, `/api/item/items`, `/api/item/item-types`, `/api/crowdsource/*` |
| Transactions endpoints | **HTTP 400 without a token** — `/api/commodity/items/{name}/transactions`, `/api/commodity/shops/{name}/transactions`, `/api/commodity/transactions`, `/api/commodity/reports`. They declare no `security` in the spec but reject unauthenticated calls anyway (probed 2026-10-29) |
| `POST /api/tools/buyers` | **required `token` header** |
| `POST /api/tools/circuits/{tradeId}` | **required `token` header** |
| `POST /api/tools/itinerary` | **required `token` header** |
| Token source | granted to [Patreon supporters](https://www.patreon.com/cw/sc_trade_tools/membership) |
| Upstream also offers | a remote MCP endpoint at `https://sc-trade.tools/mcp` (HTTP, stateless, Bearer) |

So: **without a token you get the reference + crowd-source data; with one you also get route
planning.** The server registers every tool either way and returns a clear "token not set"
message for the planning tools instead of silently failing.

## Install

```bash
cd mcp/sc-trade-tools-mcp
npm install
npm run build
```

Run:

```bash
node dist/src/cli.js
```

Register with Claude Code (repo-local `.mcp.json`):

```json
{
  "mcpServers": {
    "sc-trade-tools": {
      "command": "node",
      "args": ["mcp/sc-trade-tools-mcp/dist/src/cli.js"],
      "env": { "SC_TRADE_TOOLS_TOKEN": "…optional…" }
    }
  }
}
```

## Environment

| Var | Default | Purpose |
|---|---|---|
| `SC_TRADE_TOOLS_TOKEN` | — | Bearer token for the three planning endpoints |
| `SC_TRADE_TOOLS_BASE_URL` | `https://sc-trade.tools` | override (self-host / proxy) |
| `SC_TRADE_TOOLS_TIMEOUT_MS` | `30000` | per-request timeout |

## Tools

Read-only (no token):

- `stt_list_commodities` — all commodity names
- `stt_list_locations` — all locations with type
- `stt_list_ships` — ships + `maxBoxSizeInScu`
- `stt_list_security_levels`, `stt_list_factions`
- `stt_get_commodity_transactions` — buy/sell listings for one commodity
- `stt_get_shop_transactions` — listings at one shop
- `stt_get_crowdsource_listings` — community-submitted prices (paginated, raw)
- `stt_stock_range` — **supply range & trend** for a commodity at a location: quantity range,
  saturation range, submission window and a stable / refilling / draining verdict.
  This is the one thing UEX cannot do (UEX only exposes a discrete 1–7 status).
  Example: `titanium @ baijini` reads 5,917 → 5,782 → 5,000 over three submissions = *draining*.
- `stt_get_trade_alternatives` — alternative commodities for a trade edge

Planning (token required):

- `stt_find_best_buyers` — best places to sell what you're carrying
- `stt_plan_itinerary` — multi-stop itinerary for ship + budget
- `stt_build_circuit` — closed-loop route around a trade

The planning forms have many required fields upstream; this server fills sane defaults
(`blacklist` + empty lists = no restriction, `minSecurityLevel 0`, `supportedBoxSizeInScu 32`,
`maxStops 3`, `profitType pure`, `smartFilters true`) and lets you override any of them.

## Relationship to sc-trader (the skill in this repo)

Different data, deliberately not merged:

- `sc-trader` plans from **UEX** via `tools/uexinfo` and the scripts in `skills/sc-trader/`.
- SC Trade Tools prices are **player crowd-sourced** — a separate dataset.
- Agreed authority order in this repo: **player observation > UEX > sc-trade.tools**.

Use this server to cross-check UEX (especially crowd-sourced listings UEX may lack), not to
replace `sc-trader`'s planning. Nothing here overrides a price the user actually saw in game.
