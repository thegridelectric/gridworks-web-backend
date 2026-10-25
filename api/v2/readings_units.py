"""How the readings csv and the readings bundle put the `reading_channels`
rows of one channel into one column (the csv) or one series (the bundle).

A channel's rows are its lineage: rows joined by a shared name or a shared
`channel_id` (the layout's channel Id). A layout that changes how the
quantity is expressed (hp-lwt in WaterTempCTimes1000, then in
CelsiusTimes100) mints a new Id under the same name; a rename (vdc-relay1 to
vdc-relay) keeps the Id under a new name; a state machine's rows differ in
enum version. Each reader emits one column per lineage, named by its newest
row and in that row's unit, which is what the house sends now, with every
older row's values converted into it. A state channel's stored position
becomes the state's symbol in the row's enum version. Rows of one lineage
whose units do not convert into each other are a data fault, and the request
is refused rather than the column silently mixing scales.
"""

import importlib
import pkgutil
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import NamedTuple
from uuid import UUID

import api.sema.enums as sema_enums
import api.sema.enums.old_versions as old_enums
from api.sema.enums.gw1_unit import Gw1Unit
from api.sema.enums.gw_str_enum import SemaEnum
from api.sema.enums.spaceheat_telemetry_name import SpaceheatTelemetryName


class Scale(NamedTuple):
    """A unit as an affine scale on its quantity's reference unit (celsius
    for temperatures, gpm for flows): the stored value is
    `per_reference * reference + offset`."""

    quantity: str
    per_reference: float
    offset: float


def celsius_scale(per_degree: float) -> Scale:
    return Scale("temperature", per_degree, 0.0)


def fahrenheit_scale(per_degree: float) -> Scale:
    return Scale("temperature", per_degree * 9 / 5, 32 * per_degree)


def gpm_scale(per_gpm: float) -> Scale:
    return Scale("flow", per_gpm, 0.0)


class Affine(NamedTuple):
    """The conversion of a stored value from one unit into another:
    `slope * value + intercept`. Affine so a database query can apply it
    before aggregating."""

    slope: float
    intercept: float

    def apply(self, value: int) -> float:
        return self.slope * value + self.intercept


IDENTITY = Affine(1.0, 0.0)


def affine_between(source: Scale, target: Scale) -> Affine:
    """The map from a value stored in `source` to the same quantity stored
    in `target`."""
    slope = target.per_reference / source.per_reference
    return Affine(slope, target.offset - slope * source.offset)


# The units that convert into one another. A unit absent here converts only
# to itself.
SCALES: dict[str, Scale] = {
    SpaceheatTelemetryName.WaterTempCTimes1000.value: celsius_scale(1000),
    SpaceheatTelemetryName.AirTempCTimes1000.value: celsius_scale(1000),
    SpaceheatTelemetryName.CelsiusTimes100.value: celsius_scale(100),
    SpaceheatTelemetryName.WaterTempFTimes1000.value: fahrenheit_scale(1000),
    SpaceheatTelemetryName.AirTempFTimes1000.value: fahrenheit_scale(1000),
    Gw1Unit.FahrenheitX100.value: fahrenheit_scale(100),
    Gw1Unit.FahrenheitX10.value: fahrenheit_scale(10),
    SpaceheatTelemetryName.GpmTimes100.value: gpm_scale(100),
    Gw1Unit.GpmX100.value: gpm_scale(100),
}


def vendored_enums() -> list[type[SemaEnum]]:
    """Every enum the snapshot vendors: the current version of each and the
    old versions kept beside them."""
    modules = [sema_enums] + [
        importlib.import_module(f"{old_enums.__name__}.{m.name}")
        for m in pkgutil.iter_modules(old_enums.__path__)
    ]
    return [
        e
        for module in modules
        for e in vars(module).values()
        if isinstance(e, type)
        and issubclass(e, SemaEnum)
        and e is not SemaEnum
        and e.enum_name.__func__ is not SemaEnum.enum_name.__func__  # type: ignore[attr-defined]
    ]


ENUMS_BY_NAME_AND_VERSION: dict[tuple[str, str], type[SemaEnum]] = {
    (e.enum_name(), e.enum_version()): e for e in vendored_enums()
}
# The newest vendored version of each enum: what a state row stamped
# "Enum" (journaled before the enum version was recorded) resolves through.
ENUMS_BY_NAME: dict[str, type[SemaEnum]] = {
    name: max((e for (n, _), e in ENUMS_BY_NAME_AND_VERSION.items() if n == name), key=lambda e: e.enum_version())
    for name in {n for n, _ in ENUMS_BY_NAME_AND_VERSION}
}
UNVERSIONED_STATE_UNIT = "Enum"
# The unit_type of a numeric row names the vocabulary its unit comes from;
# every other unit_type is a state machine's enum, whose unit is the version.
UNIT_VOCABULARIES = {SpaceheatTelemetryName.enum_name(), Gw1Unit.enum_name()}
# A telemetry name that claims no scale: a value stored under it cannot be
# put in any column's unit, so the csv leaves those rows out.
NO_SCALE = SpaceheatTelemetryName.Unknown.value


class ChannelUnit(NamedTuple):
    """One `reading_channels` row as the csv query sees it. `id` is the row's
    primary key, which readings point at; `channel_id` is the layout's
    channel Id, None on a row no layout Id names (a state-machine pseudo row,
    a unit history gave no Id of its own); `retired_at` is the row's
    deactivated_date, None for the active row."""

    id: UUID
    name: str
    unit_type: str
    unit: str
    retired_at: datetime | None
    channel_id: UUID | None


class Column(NamedTuple):
    """One csv column: a channel's lineage. `name` is the newest row's name
    and heads the column; `names` is every name the lineage has had, which
    a request may ask for the column by."""

    name: str
    names: frozenset[str]
    rows: tuple[ChannelUnit, ...]


class ColumnValue(NamedTuple):
    """How a reading on one row becomes a cell: the column it belongs to,
    the column's unit (an enum name for a state column) and the conversion
    of the stored integer into it. A state cell is the state's symbol, so
    `is_state` columns take the bucket's last value where numeric columns
    take the mean. A numeric rule's conversion is also given as `affine`,
    for a reader that converts inside its query; a state rule has none."""

    column: str
    column_unit: str
    convert: Callable[[int], float | str]
    is_state: bool
    affine: Affine | None


class UnitConflict(ValueError):
    """Rows of one lineage whose units do not convert into one another."""


def newest_row(rows: Sequence[ChannelUnit]) -> ChannelUnit:
    """The active row, else the most recently retired one."""
    return max(rows, key=lambda r: (r.retired_at is None, r.retired_at or datetime.min.replace(tzinfo=UTC)))


def lineages(rows: Sequence[ChannelUnit]) -> list[Column]:
    """The columns of a set of rows: the connected sets under "same name" or
    "same channel_id", named by their newest row. Rows with no scale join
    no lineage; the csv leaves them out."""
    scaled = [r for r in rows if r.unit != NO_SCALE]
    parent = list(range(len(scaled)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(i: int, j: int) -> None:
        parent[root(i)] = root(j)

    first_by_key: dict[object, int] = {}
    for i, row in enumerate(scaled):
        for key in (("name", row.name), ("channel_id", row.channel_id)) if row.channel_id else (("name", row.name),):
            if key in first_by_key:
                join(i, first_by_key[key])
            else:
                first_by_key[key] = i
    members: dict[int, list[ChannelUnit]] = {}
    for i, row in enumerate(scaled):
        members.setdefault(root(i), []).append(row)
    return [
        Column(name=newest_row(group).name, names=frozenset(r.name for r in group), rows=tuple(group))
        for group in members.values()
    ]


def is_regex(requested: str) -> bool:
    """A requested channel of the form `^...$` is a pattern over names."""
    return len(requested) >= 2 and requested[0] == "^" and requested[-1] == "$"


def select_columns(columns: Sequence[Column], requested_channels: Sequence[str]) -> list[Column]:
    """The columns a request names, in the request's order. An exact name
    matches a column by any name its lineage has had, so a reader asking by
    the old name of a renamed channel gets the column; a regex (`^...$`)
    likewise, with its matches sorted by capture group, numerically where
    the group is digits. `all-data` is every column by name."""
    if "all-data" in requested_channels:
        return sorted(columns, key=lambda c: c.name)
    selected: list[Column] = []
    for c in requested_channels:
        if is_regex(c):
            matched: list[tuple[re.Match[str], Column]] = []
            for column in columns:
                for name in sorted(column.names, key=lambda n: n != column.name):
                    m = re.fullmatch(c, name)
                    if m is not None:
                        matched.append((m, column))
                        break
            matched.sort(key=lambda r: (*[int(g) if g.isdigit() else g for g in r[0].groups()], r[1].name))
            selected.extend(column for _, column in matched)
        else:
            selected.extend(column for column in columns if c in column.names)
    return selected


def numeric_columns(columns: Sequence[Column]) -> list[Column]:
    """The columns whose newest row is in a unit vocabulary. A state column
    has no unit a readings series can be labelled with."""
    return [c for c in columns if newest_row(c.rows).unit_type in UNIT_VOCABULARIES]


def state_value(row: ChannelUnit, column: str) -> ColumnValue:
    """A state row the snapshot cannot resolve (an enum or version it does
    not vendor) is a UnitConflict: the csv would otherwise print positions
    in an enum nobody can look up."""
    if row.unit == UNVERSIONED_STATE_UNIT:
        enum_type = ENUMS_BY_NAME.get(row.unit_type)
    else:
        enum_type = ENUMS_BY_NAME_AND_VERSION.get((row.unit_type, row.unit))
    if enum_type is None:
        raise UnitConflict(
            f"{row.name}: state enum {row.unit_type} version {row.unit} is not in this service's sema snapshot"
        )
    values = enum_type.values()

    def symbol(position: int) -> float | str:
        # A position outside the enum is the journal's refusal fallback;
        # it stays an integer so it is visibly not a state.
        return values[position] if 0 <= position < len(values) else position

    return ColumnValue(column=column, column_unit=row.unit_type, convert=symbol, is_state=True, affine=None)


def numeric_value(row: ChannelUnit, column: str, column_unit: str) -> ColumnValue:
    if row.unit == column_unit:
        affine = IDENTITY
    else:
        source, target = SCALES.get(row.unit), SCALES.get(column_unit)
        if source is None or target is None or source.quantity != target.quantity:
            raise UnitConflict(f"{column}: no conversion from {row.unit} ({row.name}) to {column_unit}")
        affine = affine_between(source, target)
    return ColumnValue(column=column, column_unit=column_unit, convert=affine.apply, is_state=False, affine=affine)


def column_values(columns: Sequence[Column]) -> dict[UUID, ColumnValue]:
    """The cell rule for every row of the given columns, keyed by row id. A
    numeric column's unit is its newest row's; a state column's cells are
    symbols whatever the version behind each row."""
    result: dict[UUID, ColumnValue] = {}
    for column in columns:
        states = [r for r in column.rows if r.unit_type not in UNIT_VOCABULARIES]
        if states and len(states) != len(column.rows):
            raise UnitConflict(f"{column.name} has both state and numeric rows")
        if states:
            result.update({r.id: state_value(r, column.name) for r in column.rows})
        else:
            column_unit = newest_row(column.rows).unit
            result.update({r.id: numeric_value(r, column.name, column_unit) for r in column.rows})
    return result
