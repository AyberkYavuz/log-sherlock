# LogSherlock in Docker

The whole stack — PostgreSQL, the FastAPI API, the offline mock LLM provider and
the React bundle behind nginx — as four containers on one network.

```bash
docker compose build
docker compose up -d
open http://localhost:3000
```

That is the entire setup. There is no schema step: the API's lifespan hook
verifies the `investigations` table on every boot, non-destructively, after
Compose has waited for PostgreSQL to report healthy.

This document covers the containers only. The graph is documented in
[`GRAPH_README.md`](GRAPH_README.md), the API in
[`BACKEND_README.md`](BACKEND_README.md), the client in
[`FRONTEND_README.md`](FRONTEND_README.md).

---

## The four services

| Service | Image | Published | Role |
| --- | --- | --- | --- |
| `postgres` | `postgres:16-alpine` | `55432` → 5432 | The only stateful service. Named volume, `pg_isready` healthcheck |
| `mock-llm` | built, target `mock-llm` | `8000` | `tests/mock_local_llm.py` — the `local` provider, offline, no API key |
| `backend` | built, target `backend` | `8010` | `python3 backend.py`. Verifies the schema on startup |
| `frontend` | built from `frontend/` | `3000` → 80 | nginx over `dist/`, plus a reverse proxy for `/api` |

**PostgreSQL is on 55432, not 5432, and that is deliberate.** A developer
running PostgreSQL locally already owns 5432, and a stack that cannot start
beside the tools it was built with is a stack nobody runs. Nothing inside the
network uses the published port — the API reaches `postgres:5432` directly.

```
   browser
     │
     ├── :3000 ──► frontend (nginx) ──┐   /api proxied, same-origin
     │                                │
     └── :8010 ──────────────────────► backend ──► postgres:5432   (volume)
                                          │
                                          └─────► mock-llm:8000    (/v1)
```

`mock_llm` and `mock-llm` both resolve, via a network alias. The code and
`.env.example` use the hyphen; the underscore is what a reader coming from a
service list reaches for first, and an alias costs nothing.

---

## Two images, three services

`backend` and `mock-llm` are two **targets** of the root `Dockerfile` over a
shared `base` stage, not two Dockerfiles. They are the same source tree —
`tests/mock_local_llm.py` imports `graph_library.env_files` and answers the same
Pydantic schemas the graph's nodes send — and they need the same interpreter and
the same dependency set. Two files would install that set twice, cache it twice,
and drift apart the first time one was edited. What actually differs is one
`CMD`, so one `CMD` is what differs.

```bash
docker build --target backend  -t logsherlock-backend .
docker build --target mock-llm -t logsherlock-mock-llm .
```

The backend image installs every provider extra (`openai`, `anthropic`,
`gemini`, `search`) plus `dev`. All four because `llm_factory` imports provider
SDKs lazily, so an image shipping one vendor would fail on a run the API
happily accepts; `dev` because it is not only about tests —
`graph_library/stats/aggregations.py` imports pandas at run time. That puts the
Python images at ~470 MB each (they share every layer, so the disk cost is one
of them, not two). The frontend is 50 MB: a multi-stage build throws the Node
toolchain away and ships static files under nginx.

`python3 backend.py` is the command rather than a bare `uvicorn` invocation,
because that entry point is what resolves and *reports* the environment file,
prints the banner naming the resolved host, port, origins and database, and
translates a failed bind into one actionable sentence.

### Why the frontend bakes no API URL

`VITE_API_BASE_URL` is built as **empty**, which makes `services/api.ts` fall
back to the relative `/api`. `frontend/nginx.conf` proxies that to
`backend:8010`, so every request is same-origin: no CORS preflight is involved
at all, and the bundle does not hardcode a host port that only happens to be
right on the machine that built it. Vite resolves `import.meta.env` during the
bundle, so there is no run-time configuration for a file already served to a
browser — override it at build time if you genuinely serve the two from
different origins:

```bash
docker compose build --build-arg VITE_API_BASE_URL=https://api.example.com frontend
```

`vite.config.ts` points `envDir` at the repository root, which is outside the
frontend build context. So the build finds no env file, says so, and the build
arg is the only source of that value — a developer's `.env` cannot leak into an
image.

Two lines of `nginx.conf` are load-bearing rather than boilerplate:
`client_max_body_size 32m`, because nginx defaults to 1 MB and the form accepts
16 MB of logs (~1.14× that once JSON-escaped); and `proxy_read_timeout 900s`, to
match `API_GRAPH_TIMEOUT` — a proxy that gave up first would fail the request in
the browser while an analysis that goes on to store perfectly well was still
running.

---

## Configuration

Two kinds of value, kept apart on purpose.

**Topology** — `API_HOST=0.0.0.0`, `DB_HOST=postgres`,
`LOCAL_LLM_BASE_URL=http://mock-llm:8000/v1`, `MOCK_LLM_HOST=0.0.0.0` — is
written literally in `docker-compose.yml`. These describe the network, not a
preference, and a developer's `.env` must not be able to reach in and change
them: `DB_HOST=localhost` is correct on a laptop and fatal in a container.

**Credentials and tuning** — provider keys, the database password, the graph
timeout, tracing — come through `${VAR:-default}`, which Compose resolves from
its environment file:

```bash
docker compose up -d                         # reads ./.env
docker compose --env-file .env.docker up -d   # reads ./.env.docker
```

Every one has a working default, so the stack also comes up on a fresh clone
with no env file at all. The LLM nodes then degrade exactly as designed, and the
`local` provider still works, because the mock needs no key.

**No env file is copied into any image.** `.dockerignore` excludes them, so a
`COPY . .` cannot bake a provider key into a layer where it survives every later
`rm` and travels with every `docker push`. Inside the container the backend
therefore reports:

```
[Config] No environment file found (looked for .env.docker or .env in: ., /).
         Reading the environment exactly as supplied, which is normal when an
         orchestrator or a secrets manager provides it.
```

That is the intended state, not a warning to fix.

`env_file:` is deliberately not used in `docker-compose.yml`. It is not optional
in Compose v2.23 (`required: false` landed in 2.24) and `.env.docker` is
gitignored, so naming it would make the file fail outright on a fresh clone.

---

## Startup ordering

```yaml
depends_on:
  postgres:
    condition: service_healthy
  mock-llm:
    condition: service_started
```

The PostgreSQL condition is what the whole file is arranged around. The lifespan
hook verifies the schema before serving a request and **aborts startup with exit
code 3** if it cannot, so starting before PostgreSQL accepts connections would
not be a slow first request — it would be a crash loop. `service_started` is not
enough: the container is up long before `initdb` has finished, which is also why
the healthcheck carries `start_period: 30s`.

`pg_isready` rather than a TCP probe, run as the configured user against the
configured database, because the container accepts connections during `initdb`
before the database is usable — and a broken credential then fails the check
instead of passing it.

`mock-llm` is `service_started`, not `service_healthy`, on purpose: it is one of
five providers and the graph degrades when a provider is unreachable, so
blocking the API's boot on a test double would give it veto over the stack.

The API's own healthcheck hits `/api/health`, which deliberately touches neither
the database nor the graph — so it reports on *that container* rather than on its
dependencies. Both Python healthchecks use `urllib` rather than curl, which the
`python:slim` image does not carry; adding a package for the sake of a probe
would be the wrong trade.

---

## Data persistence

```yaml
volumes:
  - postgres_data:/var/lib/postgresql/data
```

A **named** volume (`logsherlock_postgres_data`), managed by Docker
independently of any container's lifecycle. `docker compose down`, a rebuilt
image and a bumped `postgres` tag all leave the data in place; an anonymous
volume would be recreated empty every time.

A bind mount to a host directory would also work and is deliberately not used:
it inherits host filesystem semantics — ownership, case sensitivity, and on
Docker Desktop a virtualised share whose `fsync` behaviour PostgreSQL has no
business trusting.

> **`docker compose down -v` is the one command here that destroys data.**
> Plain `down` does not. Neither does `up --build`, `restart`, or a `docker
> compose build` followed by `up -d`.

### The verification that was run

Two records were stored — one through `POST /api/investigate` (the full
pipeline, `llm_provider: local`), one by direct `INSERT` in `psql` — then:

```bash
docker compose down     # containers and network removed, volume kept
docker compose up -d    # fresh containers, same volume
```

Both rows came back with **identical `md5(structured_report::text)` and
identical `created_at`**, so they are the same rows rather than recreated ones,
and the new boot logged:

```
[FastAPI Lifespan] Database schema verification complete on postgres:5432/postgres:
    table 'investigations' already present, 1 index(es) already present, 2 row(s) preserved
```

The `row(s) preserved` count is the point: it is the startup line that makes the
non-destructive guarantee checkable rather than merely claimed. The same two rows
also survived two full image rebuilds and container recreations.

---

## Common operations

```bash
docker compose logs -f backend                  # the startup banner and every request
docker compose logs backend | grep '\[Config\]' # which env file the container resolved
docker compose ps                               # health of all four services
docker compose exec postgres psql -U postgres   # a shell on the containerized database
docker compose exec backend python3 -m pytest -q  # the suite, inside the image
docker compose down                             # stop everything, keep the data
docker compose down -v                          # ... and delete the data
```

Running the suite inside the image is worth knowing about: it is what proves the
image is complete rather than merely bootable. Point it at a dead database first,
exactly as on the host, or the graph-level suites will store real rows:

```bash
docker compose exec -e DB_HOST=127.0.0.1 -e DB_PORT=1 backend python3 -m pytest -q
```

---

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `bind: address already in use` on 3000/8000/8010 | A local process owns the port | Change the left-hand side of the `ports:` mapping |
| `backend` exits, `Application startup failed. Exiting.` | Schema verification could not reach PostgreSQL | `docker compose logs postgres`; the backend's traceback names the driver error |
| `backend` restarts in a loop | Same as above, plus `restart: unless-stopped` doing its job | Fix the database, then `docker compose up -d` |
| Notes contain `LLM reasoning unavailable` | No provider key in the Compose environment | Use `llm_provider: "local"`, or supply the key in `.env` |
| UI loads, every request 502 | `backend` is not healthy yet, or died | `docker compose ps`; nginx proxies to a name that must resolve |
| Postgres healthy but `psql` from the host refuses | You are dialling 5432, which is the *host's* PostgreSQL | Use `-p 55432`, or `docker compose exec postgres psql` |
