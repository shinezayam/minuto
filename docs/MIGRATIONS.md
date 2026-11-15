# Database migrations (Alembic)

The schema is owned by Alembic (`app/migrations/`). `create_all()` and the old
`_add_missing_columns()` are gone. The app runs `upgrade head` on startup
(`app.db.configure`), guarded by a Postgres advisory lock so two instances never
migrate at once.

## Revisions
| id | what | downgrade |
|----|------|-----------|
| 0001 | Baseline: creates missing tables, adds legacy columns, **no-op on an up-to-date DB** | refused (would destroy data) |
| 0002 | `users.token_version INTEGER NOT NULL DEFAULT 0` | drops the column |

`app/migrations/baseline_schema.py` is a **frozen** copy of the schema at baseline time.
Never edit it; every later change is its own revision.

## Day to day
```bash
cd backend
python -m app.migrate current            # revision of DATABASE_URL (default sqlite ./minuto.db)
python -m app.migrate upgrade            # to head
python -m app.migrate downgrade 0001     # one step back (see table above)
```

## Changing the schema
1. Edit the model in `app/db.py`.
2. Write a revision in `app/migrations/versions/` (copy `0002_*.py`). **Additive first**:
   add nullable columns / columns with a server default, new tables, new indexes.
   Destructive changes (drop/rename) take two releases: stop using it, then drop.
3. Provide a working `downgrade()` and test it (`tests/test_migrations.py`).
4. `tests/test_migrations.py::test_models_and_migrations_do_not_drift` fails if the
   models and the migrated schema differ, so a forgotten revision is caught in CI.

## Before touching production
1. **Back up**: `pg_dump "$DATABASE_URL" -Fc -f minuto-$(date +%F).dump`
   (Render free Postgres has no automatic backups).
2. Check the plan: `python -m app.migrate current` against the target.
3. Deploy. Startup applies pending revisions; watch the log for
   `Migrated database X -> Y`.
4. Rollback: `python -m app.migrate downgrade <rev>` for the revisions marked
   reversible, otherwise restore the dump (`pg_restore --clean`).

## Notes / limits
* The advisory-lock path (Postgres) is not exercised by the local test suite
  (SQLite only). First production run should be watched.
* SQLite uses batch mode for ALTER; Postgres uses plain ALTER.
