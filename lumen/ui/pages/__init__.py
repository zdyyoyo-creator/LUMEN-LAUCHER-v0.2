"""页面包。"""

from .base import Page  # noqa: F401
from .browse_page import BrowsePage  # noqa: F401
from .instances_page import InstancesPage  # noqa: F401
from .modpacks_page import ModpacksPage  # noqa: F401
from .settings_page import SettingsPage  # noqa: F401
from .versions_page import VersionsPage  # noqa: F401

__all__ = ["Page", "VersionsPage", "BrowsePage", "InstancesPage", "ModpacksPage", "SettingsPage"]
