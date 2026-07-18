"""Smoke tests for E0 packages and console scripts — import-level verification only.

These tests verify that the E0 (Project Foundation) packages and console scripts
can be imported and used without network access. They test only import-level
behavior: module presence and basic attribute availability.

Add smoke tests
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _REPO_ROOT / "src"
_TOOLS_DIR = _REPO_ROOT / "tools"


class TestE0PackagesImport:
    """Verify E0 packages import without network access."""

    @pytest.mark.parametrize(
        "module_name",
        [
            "pitwall",
            "pitwall.config",
            "pitwall.api",
            "pitwall.api.app",
            "pitwall.audit",
            "pitwall.audit._runtime_config",
            "pitwall.audit.sixteen_check",
            "pitwall.mcp",
            "pitwall.migrations",
            "pitwall.reconciler",
        ],
    )
    def test_module_imports(self, module_name: str) -> None:
        """Each E0 module must be importable without network access."""
        if module_name in sys.modules:
            mod = sys.modules[module_name]
        else:
            mod = importlib.import_module(module_name)
        assert mod is not None

    def test_pitwall_version_attribute(self) -> None:
        """The pitwall package must expose a __version__ attribute."""
        import pitwall

        assert hasattr(pitwall, "__version__")
        assert isinstance(pitwall.__version__, str)

    def test_config_exports_require_runtime_env(self) -> None:
        """The config module must export require_runtime_env."""
        from pitwall.config import require_runtime_env

        assert callable(require_runtime_env)

    def test_config_exports_required_runtime_env_vars(self) -> None:
        """The config module must export required_runtime_env_vars."""
        from pitwall.config import required_runtime_env_vars

        assert callable(required_runtime_env_vars)

    def test_api_app_exports_app(self) -> None:
        """The api.app module must export an app attribute."""
        import pitwall.api.app as app_module

        assert hasattr(app_module, "app")

    def test_mcp_module_exports_server(self) -> None:
        """The mcp module must export a FastMCP server instance."""
        from pitwall.mcp import mcp

        assert mcp is not None
        assert mcp.name == "pitwall"

    def test_reconciler_module_exports_worker_settings(self) -> None:
        """The reconciler module must export WorkerSettings class."""
        from pitwall.reconciler import WorkerSettings

        assert WorkerSettings is not None
        assert hasattr(WorkerSettings, "redis_settings")
        assert hasattr(WorkerSettings, "cron_jobs")

    def test_reconciler_module_exports_validate_redis_dsn(self) -> None:
        """The reconciler module must export validate_redis_dsn function."""
        from pitwall.reconciler import validate_redis_dsn

        assert callable(validate_redis_dsn)
        assert validate_redis_dsn("redis://localhost:6379/0") is True
        assert validate_redis_dsn("not-a-dsn") is False

    def test_reconciler_module_exports_check_redis_config(self) -> None:
        """The reconciler module must export check_redis_config function."""
        from pitwall.reconciler import check_redis_config

        assert callable(check_redis_config)


# Legacy console-script smoke tests were removed: the retired status helper
# helper (which read the internal .orchestrator state) was deleted during
# open-source genericization.
