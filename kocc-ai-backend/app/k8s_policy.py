from __future__ import annotations

import re
from dataclasses import dataclass


class KubernetesPolicyViolation(ValueError):
    pass


@dataclass(frozen=True)
class ResourceRule:
    api_version: str
    kind: str
    namespaced: bool
    operations: frozenset[str]


RESOURCE_RULES = {
    ("v1", "Namespace"): ResourceRule(
        api_version="v1",
        kind="Namespace",
        namespaced=False,
        operations=frozenset({"get", "list"}),
    ),
}
DENIED_OPERATIONS = frozenset({
    "create", "update", "patch", "delete", "deletecollection", "exec",
    "attach", "portforward", "proxy", "impersonate",
})
DENIED_RESOURCE_NAMES = frozenset({"Secret", "ServiceAccountToken"})
DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?$")


def validate_k8s_request(
    *,
    operation: str,
    api_version: str,
    kind: str,
    namespace: str | None = None,
    name: str | None = None,
) -> ResourceRule:
    normalized_operation = operation.casefold()
    if normalized_operation in DENIED_OPERATIONS:
        raise KubernetesPolicyViolation("Write or unsafe operation is denied")
    if kind in DENIED_RESOURCE_NAMES or kind.casefold() in {
        "secret", "secrets", "serviceaccounts/token",
    }:
        raise KubernetesPolicyViolation("Sensitive resource is denied")
    rule = RESOURCE_RULES.get((api_version, kind))
    if rule is None or normalized_operation not in rule.operations:
        raise KubernetesPolicyViolation("Resource or operation is not allowed")
    if not rule.namespaced and namespace is not None:
        raise KubernetesPolicyViolation("Cluster-scoped resource cannot use namespace")
    if rule.namespaced and (namespace is None or not DNS_LABEL.fullmatch(namespace)):
        raise KubernetesPolicyViolation("Valid namespace is required")
    if normalized_operation == "get":
        if name is None or not DNS_LABEL.fullmatch(name):
            raise KubernetesPolicyViolation("Valid resource name is required")
    elif name is not None:
        raise KubernetesPolicyViolation("List operation cannot use resource name")
    return rule
