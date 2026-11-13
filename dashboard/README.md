# LKMBot Dashboard

This repository owns the Vue/Vuetify panel, its locked dependencies and runtime
assets. It builds without a website checkout. Source formerly hosted in the
website's bot-panel directory now lives here; the public `/bot/` entry stays the
same in the LKM stack.

From the bot repository:

```sh
pnpm --dir dashboard install --frozen-lockfile
pnpm --dir dashboard dev
pnpm --dir dashboard typecheck
node --test dashboard/tests/*.test.mjs
pnpm --dir dashboard build
```

The default build uses `/` and writes to `bot/dashboard/dist/`. Start the bot on
port 6185 to serve the API and these assets. For a reverse-proxy prefix:

```sh
VITE_BASE_PATH=/bot/ pnpm --dir dashboard build
docker build --build-arg BOT_BASE_PATH=/bot -t lkmbot .
```

The root Compose stack passes the same prefix to the bot image build, bot runtime
and gateway. Kubernetes must deploy an image built for its configured prefix.
The gateway removes the prefix for both API and static requests. Dev proxying
follows the same rule, targeting the local bot on port 6185.

`--webui-dir` and `data/dist/` deliberately override bundled assets. Move old
overrides aside before switching to an updated image. Hashed/static resources,
Shiki runtime, MDI font subsets and VAD files all come from this package.

`src/api/generated/` is generated from the bot OpenAPI definition using
`@hey-api/openapi-ts`; regenerate it when changing API contracts. `build` includes
type checking, while `build:deploy` performs asset generation and bundling.
Use `pnpm --dir dashboard i18n:check` to check the English and Simplified Chinese
message keys. Do not hardcode service origins: API, WebSocket and embedded
resource paths must pass through `src/api/base.ts`.
