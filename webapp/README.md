# Perp Hyper Arb — Monitoring Webapp

React + TypeScript dashboard for the Perp Hyper Arb trading bot. Connects to the FastAPI backend on port 8080 and provides real-time visibility into strategy state, positions, P&L, diagnostics, logs, and runtime configuration.

## Pages

| Route | Description |
|-------|-------------|
| `/` | Dashboard — bot status, P&L summary, open positions, system health |
| `/trades` | Trade history (search / filter by market, underlying, type) |
| `/positions` | Open positions, momentum positions, recently-closed spreads, and settlement/redemption panels |
| `/performance` | Analytics by market type, underlying, and strategy leg |
| `/signals` | Strategy 1/2/3 view: maker opportunities, mispricing queue, and live momentum scan diagnostics |
| `/risk` | Exposure utilization, per-coin inventory and hedge status |
| `/markets` | All monitored markets with quoting status and signal scores |
| `/fills` | Paper fill events with adversity highlighting |
| `/logs` | Live log stream plus Error History (long-lived WARNING/ERROR buffer) |
| `/settings` | All runtime config parameters, including full momentum scanner controls |

## Development

```bash
npm install
npm run dev       # dev server on http://localhost:5173
npm run build     # production build → dist/
npm run preview   # preview production build locally
```

Configure the API URL in `.env.local`:

```
VITE_API_URL=http://localhost:8080
```

## API Hooks and Endpoints

The API client lives in `src/api/client.ts` and exports a shared `BASE_URL` used by all hooks.

Notable hooks and routes added in the latest release:

- `useMomentumSignals(limit?)` → `GET /momentum/signals`
- `useMomentumDiagnostics()` → `GET /momentum/diagnostics`
- `useErrorLogs(limit?, module?, search?)` → `GET /logs/errors`
- `redeemPosition()` → `POST /positions/redeem`
- `usePolymarketEventSlugs()` now fetches via backend proxy: `GET /proxy/polymarket/events`

## Tech Stack

- React 18 + TypeScript
- Vite (dev server + bundler)
- Recharts (charts)
- React Router v6

## Original Vite template notes

Currently, two official plugins are available:

- [@vitejs/plugin-react](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react) uses [Babel](https://babeljs.io/) (or [oxc](https://oxc.rs) when used in [rolldown-vite](https://vite.dev/guide/rolldown)) for Fast Refresh
- [@vitejs/plugin-react-swc](https://github.com/vitejs/vite-plugin-react/blob/main/packages/plugin-react-swc) uses [SWC](https://swc.rs/) for Fast Refresh

## React Compiler

The React Compiler is not enabled on this template because of its impact on dev & build performances. To add it, see [this documentation](https://react.dev/learn/react-compiler/installation).

## Expanding the ESLint configuration

If you are developing a production application, we recommend updating the configuration to enable type-aware lint rules:

```js
export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...

      // Remove tseslint.configs.recommended and replace with this
      tseslint.configs.recommendedTypeChecked,
      // Alternatively, use this for stricter rules
      tseslint.configs.strictTypeChecked,
      // Optionally, add this for stylistic rules
      tseslint.configs.stylisticTypeChecked,

      // Other configs...
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```

You can also install [eslint-plugin-react-x](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-x) and [eslint-plugin-react-dom](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-dom) for React-specific lint rules:

```js
// eslint.config.js
import reactX from 'eslint-plugin-react-x'
import reactDom from 'eslint-plugin-react-dom'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...
      // Enable lint rules for React
      reactX.configs['recommended-typescript'],
      // Enable lint rules for React DOM
      reactDom.configs.recommended,
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```
