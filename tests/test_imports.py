"""Smoke test: every target module must import cleanly (iterm2 stubbed)."""
import importlib

import pytest

MODULES = [
    "server",
    "auth",
    "vault",
    "sweep_auth",
    "prune_vault",
    "reprovider",
    "scan_deploy_env",
    "icon",
    "import_auths",
    "export_auths",
]


@pytest.mark.parametrize("modname", MODULES)
def test_import(modname):
    importlib.import_module(modname)
