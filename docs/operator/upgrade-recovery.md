# Upgrade, rollback, and recovery

## Before an upgrade

Read the changelog and compatibility report, pin every container by digest, take
and verify a Postgres backup, preserve Redis if queued/idempotency state matters,
and retain webhook/archive encryption keys. Run `pitwall-gpu-broker db status` and
resolve checksum drift before changing application bytes.

Stop write traffic and background services for any release whose notes require
a maintenance window. Run exactly one migration job; the CLI also holds a
Postgres advisory lock so accidentally concurrent runners serialize. Never edit
an applied SQL file.

## Rollback

Application-only changes can roll back to the previous immutable wheel or image
digest when its schema compatibility is documented. Database migrations are
forward-only unless a release explicitly supplies and tests a reverse path.
When an incompatible migration has run, restore the pre-upgrade backup into a
new database, validate it, then point the prior application version at the
restored database. Do not improvise destructive SQL in production.

## Recovery validation

The backup drill uses credential-safe `pg_dump`/`pg_restore` invocation,
restores the complete application schema to an isolated target, and compares
every source table's row count and content checksum. A release
operator should record recovery point, recovery time, tool versions, encrypted
artifact location, and cleanup. Test passwords containing reserved URL
characters because quoting mistakes often appear only during a restore.

If a release is defective, stop publication, revoke compromised credentials,
yank (do not replace) the Python version, deprecate affected image tags/digests,
publish an advisory, and issue a new version. See `RELEASING.md` and
`docs/operator/incident-response.md`.
