# debug_routes/ package

# The legacy debug_routes.py module is shadowed by this package.
# Load it explicitly by file path so callers (server.py) can still
# do `from .debug_routes import register_debug_routes`.
# Remove after Task 8 when debug_routes.py is deleted.
import importlib.util
import os

_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "debug_routes.py")
_spec = importlib.util.spec_from_file_location("core.input._debug_routes_legacy", _path)
_legacy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_legacy)

register_debug_routes = _legacy.register_debug_routes
