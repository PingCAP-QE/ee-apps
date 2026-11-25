# TiBuild resource ownership for authorization

Status: Proposed exception for TiBuild owner review.

The TiBuild devbuild schema in `tibuild/internal/database/schema/devbuild.go`
stores a build ID and an optional createdBy string. It defines no project or
tenant relationship. The Create handler in `tibuild/internal/service/impl/devbuild.go`
uses the authenticated email when createdBy was omitted. The persistence path
in `devbuild_create.go` also accepts the supplied value. Historical values
therefore do not establish an immutable, verified owner identity.

Keep the existing TiBuild authorization for rerun, get and list. Leave resource
enforcement off until a TiBuild owner identifies an authoritative mapping from
build ID to principal, project and tenant. The checked-in static catalog is for
local integration. Product names, request metadata and createdBy cannot supply
the missing ownership relation.

Before enabling a tuple writer, record the owning system and read-only access
contract for project membership, the stable identity keys, and how removals are
observed. Specify which tuples the reconciler owns, how it detects a complete
snapshot, and how operators measure revocation delay. An incomplete or failed
read must never be interpreted as an authoritative empty membership list.
