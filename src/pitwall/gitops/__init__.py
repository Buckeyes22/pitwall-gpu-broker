"""GitOps desired-state loading, planning, and reconciliation."""

from pitwall.gitops.differ import (
    FieldChange,
    PlanAction,
    PlanEntityType,
    PlanOperation,
    ReconcilePlan,
    build_reconcile_plan,
)
from pitwall.gitops.reconcile import (
    AuditWriter,
    CapabilityRepositoryLike,
    GitOpsApplyResult,
    GitOpsDestructiveChangeError,
    ProviderRepositoryLike,
    apply_plan,
)
from pitwall.gitops.schema import (
    GITOPS_API_VERSION,
    DesiredCapabilitySpec,
    DesiredProviderSpec,
    DesiredState,
    DesiredStateDocument,
    GitOpsConfigError,
    load_desired_state,
)

__all__ = [
    "AuditWriter",
    "CapabilityRepositoryLike",
    "DesiredCapabilitySpec",
    "DesiredProviderSpec",
    "DesiredState",
    "DesiredStateDocument",
    "FieldChange",
    "GITOPS_API_VERSION",
    "GitOpsApplyResult",
    "GitOpsConfigError",
    "GitOpsDestructiveChangeError",
    "PlanAction",
    "PlanEntityType",
    "PlanOperation",
    "ProviderRepositoryLike",
    "ReconcilePlan",
    "apply_plan",
    "build_reconcile_plan",
    "load_desired_state",
]
