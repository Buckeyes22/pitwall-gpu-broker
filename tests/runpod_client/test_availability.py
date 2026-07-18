from __future__ import annotations

import time

import pytest

from pitwall.runpod_client import availability


class TestAvailabilityKey:
    def test_key_equality(self) -> None:
        key1 = availability.AvailabilityKey(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
        )
        key2 = availability.AvailabilityKey(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
        )
        assert key1 == key2

    def test_key_hashable(self) -> None:
        key = availability.AvailabilityKey(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
        )
        assert hash(key) == hash(
            availability.AvailabilityKey(
                datacenter="US-KS-2",
                gpu_name="NVIDIA H100 80GB HBM3",
                cloud_type="SECURE",
                gpu_count=1,
            )
        )

    def test_key_cloud_type_case_preserved(self) -> None:
        key = availability.AvailabilityKey(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="secure",
            gpu_count=1,
        )
        assert key.cloud_type == "secure"

    def test_key_rejects_empty_datacenter(self) -> None:
        with pytest.raises(ValueError, match="datacenter must be non-empty"):
            availability.AvailabilityKey(
                datacenter="",
                gpu_name="NVIDIA H100 80GB HBM3",
                cloud_type="SECURE",
                gpu_count=1,
            )

    def test_key_rejects_empty_gpu_name(self) -> None:
        with pytest.raises(ValueError, match="gpu_name must be non-empty"):
            availability.AvailabilityKey(
                datacenter="US-KS-2",
                gpu_name="",
                cloud_type="SECURE",
                gpu_count=1,
            )

    def test_key_rejects_empty_cloud_type(self) -> None:
        with pytest.raises(ValueError, match="cloud_type must be non-empty"):
            availability.AvailabilityKey(
                datacenter="US-KS-2",
                gpu_name="NVIDIA H100 80GB HBM3",
                cloud_type="",
                gpu_count=1,
            )

    def test_key_rejects_zero_gpu_count(self) -> None:
        with pytest.raises(ValueError, match="gpu_count must be >= 1"):
            availability.AvailabilityKey(
                datacenter="US-KS-2",
                gpu_name="NVIDIA H100 80GB HBM3",
                cloud_type="SECURE",
                gpu_count=0,
            )

    def test_key_rejects_negative_gpu_count(self) -> None:
        with pytest.raises(ValueError, match="gpu_count must be >= 1"):
            availability.AvailabilityKey(
                datacenter="US-KS-2",
                gpu_name="NVIDIA H100 80GB HBM3",
                cloud_type="SECURE",
                gpu_count=-1,
            )


class TestAvailabilityCache:
    def test_is_available_returns_none_when_empty(self) -> None:
        cache = availability.AvailabilityCache()
        result = cache.is_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
        )
        assert result is None

    def test_set_and_get_available(self) -> None:
        cache = availability.AvailabilityCache()
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        result = cache.is_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
        )
        assert result is True

    def test_set_unavailable(self) -> None:
        cache = availability.AvailabilityCache()
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=False,
        )
        result = cache.is_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
        )
        assert result is False

    def test_cloud_type_case_insensitive(self) -> None:
        cache = availability.AvailabilityCache()
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="secure",
            gpu_count=1,
            available=True,
        )
        result = cache.is_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
        )
        assert result is True

    def test_different_keys_independent(self) -> None:
        cache = availability.AvailabilityCache()
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        cache.set_available(
            datacenter="US-CA-1",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=False,
        )
        assert (
            cache.is_available(
                datacenter="US-KS-2",
                gpu_name="NVIDIA H100 80GB HBM3",
                cloud_type="SECURE",
                gpu_count=1,
            )
            is True
        )
        assert (
            cache.is_available(
                datacenter="US-CA-1",
                gpu_name="NVIDIA H100 80GB HBM3",
                cloud_type="SECURE",
                gpu_count=1,
            )
            is False
        )

    def test_bulk_set_available(self) -> None:
        cache = availability.AvailabilityCache()
        entries = [
            ("US-KS-2", "NVIDIA H100 80GB HBM3", "SECURE", 1, True),
            ("US-CA-1", "NVIDIA H100 80GB HBM3", "SECURE", 1, False),
            ("US-KS-2", "NVIDIA L4", "COMMUNITY", 1, True),
        ]
        cache.bulk_set_available(entries)
        assert cache.is_available("US-KS-2", "NVIDIA H100 80GB HBM3", "SECURE", 1) is True
        assert cache.is_available("US-CA-1", "NVIDIA H100 80GB HBM3", "SECURE", 1) is False
        assert cache.is_available("US-KS-2", "NVIDIA L4", "COMMUNITY", 1) is True

    def test_invalidate_clears_all(self) -> None:
        cache = availability.AvailabilityCache()
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        cache.invalidate()
        assert cache.is_available("US-KS-2", "NVIDIA H100 80GB HBM3", "SECURE", 1) is None

    def test_invalidate_key(self) -> None:
        cache = availability.AvailabilityCache()
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        cache.set_available(
            datacenter="US-CA-1",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=False,
        )
        assert cache.invalidate_key("US-KS-2", "NVIDIA H100 80GB HBM3", "SECURE", 1) is True
        assert cache.is_available("US-KS-2", "NVIDIA H100 80GB HBM3", "SECURE", 1) is None
        assert cache.is_available("US-CA-1", "NVIDIA H100 80GB HBM3", "SECURE", 1) is False

    def test_invalidate_key_returns_false_when_missing(self) -> None:
        cache = availability.AvailabilityCache()
        assert cache.invalidate_key("US-KS-2", "NVIDIA H100 80GB HBM3", "SECURE", 1) is False

    def test_cache_size(self) -> None:
        cache = availability.AvailabilityCache()
        assert cache.cache_size() == 0
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        assert cache.cache_size() == 1
        cache.set_available(
            datacenter="US-CA-1",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        assert cache.cache_size() == 2

    def test_len(self) -> None:
        cache = availability.AvailabilityCache()
        assert len(cache) == 0
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        assert len(cache) == 1

    def test_sweep_expired_removes_expired(self) -> None:
        cache = availability.AvailabilityCache()
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        assert len(cache) == 1
        time.sleep(0.1)
        removed = cache.sweep_expired()
        assert removed == 0

    def test_expired_count(self) -> None:
        cache = availability.AvailabilityCache()
        assert cache.expired_count() == 0
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        assert cache.expired_count() == 0


class TestGlobalCache:
    def test_get_global_returns_singleton(self) -> None:
        cache1 = availability.get_global_availability_cache()
        cache2 = availability.get_global_availability_cache()
        assert cache1 is cache2

    def test_reset_global_clears_and_returns_new(self) -> None:
        cache1 = availability.get_global_availability_cache()
        cache1.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        availability.reset_global_availability_cache()
        cache2 = availability.get_global_availability_cache()
        assert cache1 is not cache2
        assert cache2.is_available("US-KS-2", "NVIDIA H100 80GB HBM3", "SECURE", 1) is None


class TestTTL:
    def test_default_ttl_is_300_seconds(self) -> None:
        cache = availability.AvailabilityCache()
        assert cache._ttl_s == 300.0

    def test_expired_entry_returns_none(self) -> None:
        cache = availability.AvailabilityCache()
        cache._ttl_s = 0.05
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        assert cache.is_available("US-KS-2", "NVIDIA H100 80GB HBM3", "SECURE", 1) is True
        time.sleep(0.1)
        assert cache.is_available("US-KS-2", "NVIDIA H100 80GB HBM3", "SECURE", 1) is None

    def test_sweep_expired_with_short_ttl(self) -> None:
        cache = availability.AvailabilityCache()
        cache._ttl_s = 0.05
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        cache.set_available(
            datacenter="US-CA-1",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=False,
        )
        assert len(cache) == 2
        time.sleep(0.1)
        removed = cache.sweep_expired()
        assert removed == 2
        assert len(cache) == 0

    def test_update_extends_ttl(self) -> None:
        cache = availability.AvailabilityCache()
        cache._ttl_s = 1.0
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        time.sleep(0.5)
        cache.set_available(
            datacenter="US-KS-2",
            gpu_name="NVIDIA H100 80GB HBM3",
            cloud_type="SECURE",
            gpu_count=1,
            available=True,
        )
        time.sleep(0.6)
        assert cache.is_available("US-KS-2", "NVIDIA H100 80GB HBM3", "SECURE", 1) is True
