# Day 5 Glance integration harness

This local acceptance harness connects the frontend's real `GlanceApiClient` to
the real FastAPI routes through the repository's Caddyfile. PostgreSQL and
Redis come from `docker-compose.test.yml`; the harness resets only that
disposable test database. It does not use production services or credentials.

The ASR HTTP call uses the existing test harness `MockTransport`, the model is
a deterministic no-tools stub, and Telegram/WhatsApp transports are intercepted.
The seeded owners, salons, customers, messages and replies are synthetic. No
external customer message is sent.

## Run

From `remboai`, create the private host directory used for the one-time pairing
code and test signals (the API container runs as UID/GID 1000):

```sh
install -d -o 1000 -g 1000 -m 700 /tmp/remboai-glance-day5-user
docker compose -f docker-compose.test.yml -f tests/docker-compose.glance-integration.yml \
  up -d --build glance-integration-api glance-integration-caddy
until test -s /tmp/remboai-glance-day5-user/ready.json; do sleep 0.25; done
```

From `remboai-glance`, run the actual frontend API client against the local
FastAPI/Caddy stack:

```sh
node_modules/.bin/tsx tests/backend-integration.ts \
  /tmp/remboai-glance-day5-user/ready.json
```

The script checks pairing, scoped inbox/context reads, latest-ten look-ahead,
cross-salon 404 parity, AI revision, manual drafting, raw PCM transcription,
exact-text approval, idempotent replay/conflict, intercepted outbox dispatch,
reply status, resolution, CORS headers, and the 1,280,000-byte Caddy boundary.
The controlled Origin is `http://127.0.0.1:5173`, matching the local simulator
WebView origin. The Node test sends the CORS preflight and checks response
headers; it does not emulate browser CORS enforcement or establish the packaged
WebView origin.

## Stop

From `remboai`, stop the local acceptance services without removing volumes:

```sh
docker compose -f docker-compose.test.yml -f tests/docker-compose.glance-integration.yml \
  stop glance-integration-api glance-integration-caddy test-db test-redis
```

The ready file contains a short-lived pairing code and synthetic IDs. Keep its
directory mode restricted and remove the file after the run. No staging or
production deployment is involved; local Caddy listens on `127.0.0.1:18081`
over HTTP.
