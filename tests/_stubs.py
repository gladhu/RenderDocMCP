"""Load renderdoc_extension.services modules outside RenderDoc.

The extension modules import ``renderdoc`` (only available inside qrenderdoc) and the
``renderdoc_extension.utils`` package, which pulls in serializers that need the real API.
Only pure functions are tested, so a bare ``renderdoc`` module, stub packages whose
``__path__`` points at the real source folders, a dummy ``Helpers`` and a pass-through
``action_service._sd_value`` are enough for the imports to succeed.
"""

import importlib
import os
import sys
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTENSION_DIR = os.path.join(REPO_ROOT, "renderdoc_extension")
SERVICES_DIR = os.path.join(EXTENSION_DIR, "services")

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def _module(name, path=None, **attrs):
    mod = types.ModuleType(name)
    if path is not None:
        mod.__path__ = [path]
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def install():
    """Put the stubs into sys.modules (idempotent)."""
    if getattr(sys.modules.get("renderdoc_extension"), "_test_stub", False):
        return
    _module("renderdoc")
    _module("renderdoc_extension", EXTENSION_DIR, _test_stub=True)
    _module("renderdoc_extension.utils", os.path.join(EXTENSION_DIR, "utils"), Helpers=object, Parsers=object)
    _module("renderdoc_extension.services", SERVICES_DIR)
    _module("renderdoc_extension.services.action_service", _sd_value=lambda x: x)


def load_service(name):
    """Import renderdoc_extension.services.<name> with the stubs in place."""
    install()
    return importlib.import_module("renderdoc_extension.services." + name)
