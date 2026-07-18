"""S9: provider-URL SSRF — runpod_endpoint_id must be strictly validated.

``runpod_endpoint_id`` is interpolated into outbound RunPod URLs without
validation: as the *host label* for ``serverless_lb``
(``https://{id}.api.runpod.ai``) and into the *path* for ``serverless_queue``
(``https://api.runpod.ai/v2/{id}``). A hostile id therefore redirects
Pitwall's own credentialed outbound calls — host-label injection, path
traversal, or the cloud metadata IP. This pins a strict allow-list validator
at the single chokepoint ``_require_endpoint_id`` and keeps the allow-list host
invariant green before and after the fix.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable

import pytest

from pitwall.core.enums import ProviderType
from pitwall.core.models import Provider
from pitwall.resolver import provider_urls

pytestmark = pytest.mark.security


# Hostile ids that, unvalidated, escape the intended *.api.runpod.ai surface.
HOSTILE_IDS = [
    "evil.example.com",  # host-label injection (resolves to attacker DNS)
    "../../internal",  # path traversal
    "a/b",  # path injection
    "a b",  # whitespace splits the URL
    "169.254.169.254",  # cloud metadata IP
    "endpoint@evil.com",  # userinfo injection — real host becomes evil.com
    "endpoint:8080",  # port injection
    "runpod.ai.evil.com",  # suffix attack
    "x",  # implausibly short — never a real endpoint id
]


@pytest.mark.parametrize("hostile_id", HOSTILE_IDS)
def test_lb_url_rejects_hostile_endpoint_id(
    provider_factory: Callable[..., Provider], hostile_id: str
) -> None:
    provider = provider_factory(endpoint_id=hostile_id, provider_type=ProviderType.SERVERLESS_LB)
    with pytest.raises(ValueError):
        provider_urls.lb_url(provider, "/embed")


@pytest.mark.parametrize("hostile_id", HOSTILE_IDS)
def test_queue_url_rejects_hostile_endpoint_id(
    provider_factory: Callable[..., Provider], hostile_id: str
) -> None:
    provider = provider_factory(endpoint_id=hostile_id, provider_type=ProviderType.SERVERLESS_QUEUE)
    with pytest.raises(ValueError):
        provider_urls.queue_url(provider, "/runsync")


def test_empty_endpoint_id_still_rejected(
    provider_factory: Callable[..., Provider],
) -> None:
    """The pre-existing null-check must remain (regression net)."""
    provider = provider_factory(endpoint_id="", provider_type=ProviderType.SERVERLESS_LB)
    with pytest.raises(ValueError):
        provider_urls.lb_url(provider, "/embed")


def test_valid_endpoint_id_targets_only_runpod_host(
    provider_factory: Callable[..., Provider],
) -> None:
    """Allow-list invariant: a valid id only ever resolves to *.api.runpod.ai.

    Green before *and* after the fix — guards against the validator being too
    strict and breaking legitimate ids.
    """
    lb_provider = provider_factory(
        endpoint_id="eptest00000000", provider_type=ProviderType.SERVERLESS_LB
    )
    lb_host = urllib.parse.urlparse(provider_urls.lb_url(lb_provider, "/embed")).hostname
    assert lb_host == "eptest00000000.api.runpod.ai"

    queue_provider = provider_factory(
        endpoint_id="eptest00000000", provider_type=ProviderType.SERVERLESS_QUEUE
    )
    queue_host = urllib.parse.urlparse(provider_urls.queue_url(queue_provider, "/runsync")).hostname
    assert queue_host == "api.runpod.ai"
