# Code layout and contribution guide

ClawPay uses a small layered backend. Group modules by business responsibility inside each layer; keep I/O at the edges and payment invariants in the database. A file should have a coherent reason to change. Split unrelated responsibilities rather than splitting just to hit a line-count target.

## Repository map

The repository has three parts: `backend/` (this API, documented below), `frontend/` (owner and judge dashboard) and `mobile/watch/` (watchOS client). Run backend commands from `backend/`.

```text
backend/
  app/
    api/                 HTTP routes, authentication dependencies, bounded body reads
    core/                Settings, clock, logging, security and shared errors
    domain/              Pure money, canonical payload and order-state rules
    integrations/
      a2a/               Merchant HTTP protocol
      mcp/               Stateless JSON-RPC and the four-tool registry
      paypal/            Gateway protocol, service adapter, client, vault, orders, verification
      supabase/          Low-level client construction
    merchant_agents/     Standalone deterministic mock merchant ASGI apps
    repositories/
      ports/             Driver-independent interfaces and records, grouped by feature
      supabase/          Concrete service-role adapters, one file per feature
      bundle.py          Typed collection of repository capabilities
      errors.py          Shared database error/retry policy
      user_scoped.py     Separate JWT/RLS dashboard reader and connection lifetime
    schemas/             Request/response validation and safe dashboard projections
    services/            Business use cases; container.py is the composition root
    main.py              App factory, routes, lifespan and exception handlers
  docs/                  Tracked contracts, setup, architecture, status and remaining work
  scripts/               Offline SQL bootstrap generation/check command
  supabase/
    schema.sql, seed.sql  Generated SQL Editor rebuild scripts for dropped app tables
    migrations/          Ordered database changes; never rewrite an applied migration
    tests/database/      pgTAP sources for database invariants and permissions
  tests/
    fakes/
      repositories/      Feature adapters sharing one transactional in-memory store
      store.py           State, fixture creation and race/failure hooks
      approvals.py       Software-signing and approval HTTP helpers
      payments.py        Seeded payment state and mocked service construction
      webhooks.py        Mock notification fixtures and delivery helpers
    unit/                Focused behavior and repository tests
    contract/            MCP/device protocol tests against tracked contracts
    integration/         Complete mocked workflows; live smoke is explicit opt-in
```

## Dependency rules

- Routes authenticate/validate input and call services. They do not construct database clients.
- Services depend on `repositories/ports/<feature>.py`; only `services/container.py` wires concrete Supabase adapters. The domain layer has no HTTP/database imports.
- Repository ports have no driver, application-service or test imports. Shared `RpcResult` lives in `ports/common.py`; feature records stay beside their interfaces.
- The Supabase `base.py` owns thread execution and error conversion. Device, order, approval, payment-method, payment, merchant, spending, conversation/event and webhook adapters live separately. No duplicate query execution helpers per adapter.
- Service-role writes and user-JWT dashboard reads remain visibly separate. Dashboard queries use explicit safe columns and close their per-request client.
- PayPal's `gateway.py` is the protocol; `service.py` implements it. The package initializer only exposes the public API. Provider payload parsing stays inside the integration.
- Test support never imports a test module. Reusable fixtures/helpers belong under `tests/fakes/`; fake adapters depend on store types only during type checking, avoiding import cycles.
- `docs/` is the canonical documentation location and ships with the repository. The old workspace `../doc/` pages only forward here. Tests must not depend on ignored `.agents/` files or the parent workspace.

## Adding a feature

1. Define the smallest required record/interface in the relevant `ports/` module.
2. Implement the database adapter in the matching `supabase/` module. Add it to `bundle.py` and wire it in the container only if a new capability is required.
3. Implement the matching fake against shared state/lock. Keep reason codes aligned with SQL and use failure/race hooks to cover concurrency invariants.
4. Put orchestration in a service, input/output validation in schemas and routing/authentication in the API layer.
5. Add meaningful unit/contract/integration coverage. Default tests must use MockTransport or ASGITransport and never need real credentials, a database or network.
6. Update contracts/setup/remaining-work when behavior, configuration or operational steps change. An implemented backend is different from live-validated behavior; record both honestly.

## Maintenance checks

```powershell
uv sync --offline --python 3.12
uv run --offline pytest
uv run python -c "from app.core.config import Settings; from app.main import create_app; create_app(Settings(_env_file=None))"
git diff --check
```

Editors follow `.editorconfig`; Git normalizes new text files with `.gitattributes`. The initial migration is explicitly excluded from normalization to preserve its applied checksum. Keep Python type annotations on public service/repository boundaries, standard import grouping and readable multiline calls. Avoid broad `Any` where a repository protocol or record is available. Do not introduce abstraction layers without a concrete shared responsibility.

Secrets, local virtual environments/caches and agent scratch notes are ignored. Documentation and tests are tracked. Do not copy credentials into fixtures. Run the server with one worker until in-memory limits/budgets/throttles have a shared implementation. Optional live smoke and database deployment remain deliberate operator actions.

If migrations change, regenerate the standalone SQL scripts and run the generation drift checks. See [database rebuild](database-rebuild.md). The schema file is a post-drop bootstrap, not an incremental migration or a destructive reset.
