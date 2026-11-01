from enum import auto

from api.sema.enums.gw_str_enum import SemaEnum


class CommitStanding(SemaEnum):
    """Sema: https://schemas.electricity.works/enums/gw.commit.standing/000"""

    Unknown = auto()
    OnMainHead = auto()
    BehindMain = auto()
    OffMain = auto()
    Dirty = auto()

    @classmethod
    def default(cls) -> "CommitStanding":
        return cls.Unknown

    @classmethod
    def values(cls) -> list[str]:
        return [elt.value for elt in cls]

    @classmethod
    def enum_name(cls) -> str:
        return "gw.commit.standing"

    @classmethod
    def enum_version(cls) -> str:
        return "000"
