"""Map export paths to metadata owners and module kinds."""

from dataclasses import dataclass
from pathlib import PurePosixPath

from open1c_analyzer.parser.names import DIRECTORY_KIND_MAP, full_metadata_name

MODULE_KINDS = {
    "CommandModule": "command_module",
    "ExternalConnectionModule": "external_connection_module",
    "ManagerModule": "manager_module",
    "ManagedApplicationModule": "managed_application_module",
    "Module": "module",
    "ObjectModule": "object_module",
    "OrdinaryApplicationModule": "ordinary_application_module",
    "RecordSetModule": "record_set_module",
    "SessionModule": "session_module",
    "ValueManagerModule": "value_manager_module",
}


@dataclass(frozen=True, slots=True)
class ModuleIdentity:
    name: str
    full_name: str
    module_kind: str
    owner_kind: str | None
    owner_name: str | None


def identify_module(relative_path: str) -> ModuleIdentity:
    path = PurePosixPath(relative_path.replace("\\", "/"))
    parts = list(path.parts)
    module_kind = MODULE_KINDS.get(path.stem, "module")
    owner_kind: str | None = None
    owner_name: str | None = None

    # Managed form modules live below ``.../Forms/<FormName>/Ext/Form/Module.bsl``.
    # Treat them as first-class modules instead of collapsing every form module into
    # the document/catalog owner.  The previous identity (for example
    # ``Документ.РасходнаяНакладная.module``) made it impossible for retrieval to
    # distinguish a list form from a manager/object module.
    if "Forms" in parts:
        forms_index = parts.index("Forms")
        if forms_index > 1 and forms_index + 1 < len(parts):
            form_name = parts[forms_index + 1]
            for index, part in enumerate(parts[:forms_index]):
                if part in DIRECTORY_KIND_MAP and index + 1 < forms_index:
                    owner_kind = DIRECTORY_KIND_MAP[part]
                    owner_name = parts[index + 1]
                    break
            if owner_kind and owner_name:
                owner = full_metadata_name(owner_kind, owner_name)
                return ModuleIdentity(
                    f"{form_name}.form_module",
                    f"{owner}.Form.{form_name}.form_module",
                    "form_module",
                    owner_kind,
                    owner_name,
                )

    for index, part in enumerate(parts[:-1]):
        if part in DIRECTORY_KIND_MAP and index + 1 < len(parts):
            owner_kind = DIRECTORY_KIND_MAP[part]
            owner_name = parts[index + 1]
            break
    if owner_kind == "common_module" and owner_name:
        return ModuleIdentity(owner_name, owner_name, module_kind, owner_kind, owner_name)
    if owner_kind and owner_name:
        owner = full_metadata_name(owner_kind, owner_name)
        return ModuleIdentity(
            f"{owner_name}.{module_kind}",
            f"{owner}.{module_kind}",
            module_kind,
            owner_kind,
            owner_name,
        )
    return ModuleIdentity(path.stem, relative_path, module_kind, None, None)
