# eugene-plexus/identity

Eugene's sense of self. The Default Mode Network (DMN) analogue in the
Eugene Plexus consciousness framework — the component that holds the
parts of Eugene's mind that constitute who he is, distinct from the
hemispheres that do the thinking.

Two internal nodes mirror the DMN's split:

- **Constitution** (medial prefrontal cortex / mPFC) — the immutable,
  declarative half. "I am Eugene." Name, pronouns, core values,
  operator-supplied backstory. Operator-editable from the UI; Eugene
  cannot modify this. Stored as YAML on disk for easy inspection.

- **Self-model** (posterior cingulate cortex / precuneus) — the
  autobiographical, mutable half. Reflections Eugene writes about
  himself over time. Stored in SQLite, queried by topic relevance.

Plus the **persons** layer — anyone Eugene interacts with via any
platform — and the **pending-links** flow that brings new platform
identities into the persons graph under operator approval.

See [`eugene-plexus/specs`](https://github.com/eugene-plexus/specs) /
`openapi/identity.yaml` for the full HTTP contract.

## Status

v0.2 skeleton. Endpoints implemented:

- ✅ `GET` / `PATCH /v1/identity/constitution`
- ✅ `GET /v1/identity/self-model`
- ✅ `GET` / `POST` / `PATCH` / `DELETE /v1/identity/persons/...`
- ✅ `GET /v1/identity/persons/{id}/relationship`
- ✅ `GET` / `POST /v1/identity/links/pending`
- ✅ `POST /v1/identity/links/pending/{id}/approve` / `/reject`
- ✅ Standard config trio (`/v1/config`, `/v1/config/schema`)
- ✅ `/healthz`
- ✅ `POST /v1/admin/restart`
- ⏳ `POST /v1/identity/self-model/reflect` — returns 501 in v0.2;
  needs a configured hemisphere-driver to actually reflect, lands as
  a follow-up.

v0.2 auth: every endpoint except `/healthz` validates the watchdog-
issued bearer JWT. Constitution PATCH + persons mutations + link
approval are operator-audience only. Filing a new pending link
(`POST /v1/identity/links/pending`) is service-audience only — only
connector adapters can introduce new identities.

## Development

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest
```

Regenerate models from a new specs commit:

```sh
echo "<new-sha>" > SPECS_REF
.venv/Scripts/python scripts/codegen.py
```

## Storage

- Constitution: `~/.eugene-plexus/identity/constitution.yaml`
  (path configurable). Loaded into memory at startup, written back
  on every successful PATCH.

- Persons, platform aliases, self-model entries, pending links:
  SQLite database at `~/.eugene-plexus/identity/identity.db` (path
  configurable). One file, single-writer concurrency model — fine
  for the personal-install scale; if a future deployment needs
  multi-writer, the storage layer is isolated behind a Protocol so
  swapping backends is a focused change.

The wizard's first-run flow creates the operator's `Person` record
automatically when it calls `POST /v1/auth/initialize` — see the
watchdog repo. The operator's `personId` is always the same UUID
across this install's lifetime.
