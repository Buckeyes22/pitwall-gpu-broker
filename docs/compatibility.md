# Compatibility policy

Pitwall follows semantic versioning, but `0.x` releases are alpha.
Python 3.12 and 3.13 on Linux are supported. Postgres 16 and Redis 7 are the
canonical deployment versions for the first alpha. Other operating systems are
not claimed until they have a maintained CI lane.

Within a published patch line, documented REST paths, MCP tool names, CLI entry
points, database migrations, and webhook envelope version 1 should remain
backward compatible. A pre-1.0 minor release may make breaking changes only when
the changelog identifies them, the OpenAPI compatibility check records them,
and an upgrade/rollback path is documented. Applied migrations are immutable;
checksum drift is a hard error.

Security fixes may disable or remove an unsafe behavior without a deprecation
period. Published package versions and image digests are never overwritten.
Removal of a supported surface requires an ADR and support/capability matrix
update. Experimental or deferred surfaces carry no compatibility promise.

The release workflow validates tag, package version, changelog date, artifact
contents, and installability. OpenAPI paths, methods, required fields, and
successful response schemas are compared with the previous release when one
exists; approved breaking changes require a new minor alpha version.

Every required workflow installs the frozen lock. A scheduled compatibility job
also resolves both the highest compatible and lowest-direct dependency graphs
and runs the hermetic suite, so declared ranges are exercised without making
pull requests depend on an unreviewed resolver result.
