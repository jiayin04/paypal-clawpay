# Recreate ClawPay after dropping its tables

Use this path when the ClawPay application tables are already gone. For an intact database, use the normal migration workflow instead. These files target a normal Supabase project with its Auth table, platform roles and system schemas intact. They do not recreate Supabase Auth/Storage itself.

## Apply the two files

1. Stop the backend while the database is being recreated.
2. In the Supabase dashboard, open **SQL Editor** and use the **postgres** database role. Open [schema.sql](../supabase/schema.sql), paste the entire file into a new query and run it. It includes all five migrations (initial through phase 4) in order inside one transaction. Do not also replay those migrations individually.
3. Once schema.sql succeeds, paste and run the entire [seed.sql](../supabase/seed.sql) in a separate query. It is also transactional and safe to rerun. It inserts Grocer Demo / Electronics Demo and recreates missing profiles for any users still in `auth.users`.
4. Check the seed results: merchant codes `grocer` and `electronics` should exist, and existing Auth users should have profile rows. If there are no Auth users, create/sign in a user through Supabase Auth; the signup trigger creates the profile automatically.
5. Restart the backend and check `/health/ready`. Register your device again, store its newly returned `cpd_`/`cpl_` tokens, and link your own Sandbox PayPal wallet again. Dropping the tables erased the previous device-token hashes, wallet references, orders, approvals and spending history; the seed does not invent replacement financial records.

The mock products and prices live in `app/merchant_agents/catalogs.py`, not a products table. The seed intentionally contains no fake Auth users, devices, public/private signing keys, raw tokens, vaulted wallets, approvals, orders or payments.

## Guardrails and errors

- The schema refuses to run if any of the 16 ClawPay relations still exist. It does not drop/reset tables or use CASCADE. A second run after success will therefore stop; this is expected.
- If `auth.users` or the Supabase roles are missing, stop and restore the Supabase platform/project prerequisites. Do not create fake Auth system tables.
- Dropping public tables can leave ClawPay functions and `on_auth_user_created` attached to `auth.users`. The script removes those known function signatures and recreates the signup trigger; a same-name trigger owned by another integration or a foreign dependency causes a failure instead of cascading deletion.
- If a statement fails, the schema transaction cannot commit partial work. If your SQL session reports an aborted transaction, issue `ROLLBACK;`, retain the error text and fix its cause before rerunning the complete file. Do not run only the tail of the script.
- Database privileges are reapplied explicitly, RLS is checked before commit, application RPC execution is denied to client roles, and PostgREST receives a schema-cache reload notification.

## Migration history after manual application

Dropping tables does not necessarily remove `supabase_migrations.schema_migrations`. These standalone files do **not** edit that managed history. Their effective schema represents all of these versions:

- `20261024133807`
- `20261025090000`
- `20261025090100`
- `20261025090200`
- `20261025090300`

Before a future CLI push, inspect `supabase migration list` and reconcile history with the schema that actually succeeded. If history contains only the initial version, blindly pushing the remaining migrations after this full bootstrap would try to add existing columns/tables again. Mark a version applied only after verifying that its effects are already present; do not use history repair as a replacement for applying the schema. The original migration files remain unchanged.

## Maintain the generated files

Migrations are the source of truth. The helper only reads repository files; it has no database/network code. From the backend directory:

```powershell
uv run python scripts/generate_database_bootstrap.py
uv run python scripts/generate_database_bootstrap.py --check
```

The generator keeps all migration DDL/function bodies/privileges, moves the existing merchant seed into seed.sql and adds the empty-schema prerequisites, stale-function cleanup, explicit service-role table access, Auth-profile backfill and transactional checks. Source filenames and LF-normalized SHA-256 values are recorded in schema.sql. Do not hand-edit generated SQL or an applied migration.

Generated-file/content checks are offline verification, not execution against your project. No hosted database or real PayPal operation was performed by the coding agent.
