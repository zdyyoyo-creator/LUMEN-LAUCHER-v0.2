"""整合包模块：Modrinth .mrpack / CurseForge manifest / 通用 zip。"""

from .curseforge import CurseManifest, read_curse_manifest, resolve_files  # noqa: F401
from .importer import (  # noqa: F401
    ImportProgress,
    ImportReport,
    ModpackPlan,
    import_modpack,
    inspect_modpack,
)
from .mrpack import MrpackIndex, read_mrpack  # noqa: F401

__all__ = [
    "read_mrpack",
    "MrpackIndex",
    "read_curse_manifest",
    "CurseManifest",
    "resolve_files",
    "inspect_modpack",
    "import_modpack",
    "ModpackPlan",
    "ImportProgress",
    "ImportReport",
]
