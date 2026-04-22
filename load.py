"""
EDMC entrypoint for the Squadrons Telemetry plugin.

EDMC discovers plugins by importing ``plugins/<PluginName>/load.py`` and
looking up its hook functions (``plugin_start3``, ``plugin_stop``,
``plugin_prefs``, ``prefs_changed``, ``journal_entry``) on that module's
namespace. The implementation lives in ``squadrons_telemetry.py``; this
file just re-exports the hooks so EDMC can see them.
"""

from squadrons_telemetry import (  # noqa: F401
    journal_entry,
    plugin_prefs,
    plugin_start3,
    plugin_stop,
    prefs_changed,
)
