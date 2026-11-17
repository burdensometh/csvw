"""
Merge rows of a TableGroup's data that describe the same entity.

After cleaning tabular data it frequently turns out that several *business keys*
(i.e. primary key values) actually denote the same real-world entity. To merge
the corresponding rows properly it is not enough to update the metadata: all
rows of the referenced table as well as **every** foreign key pointing at the
merged keys - including list-valued foreign keys (which end up in association
tables when loading the data into SQLite) and foreign keys on tables that are
only related indirectly - have to be rewritten.

The merge is driven by an explicit mapping of old (i.e. possibly duplicate)
keys to a *canonical* key per table. Mappings may form chains
(``a -> b -> c``) which are resolved transitively, but cycles are rejected.

Keys are resolved according to the datatype declared for the corresponding
primary key column(s), so scalar (single-column) and composite (multi-column)
primary keys are both supported.

Non-key attributes of merged rows are combined conservatively: two values are
only merged if they are equal or one side is empty; any other clash aborts the
whole batch with a :class:`MergeError`. List-valued foreign-key columns are the
exception: their (rewritten) entries are concatenated in row order, preserving
order and duplicates.
"""
import copy
import dataclasses
import collections
from typing import Optional, Union, Any

from csvw.metadata import TableGroup, Table

__all__ = ['MergeError', 'KeyMap', 'merge_tablegroup', 'check_referential_integrity']


class MergeError(ValueError):
    """Raised when data cannot be merged according to the specified key maps."""


@dataclasses.dataclass
class KeyMap:
    """
    An explicit mapping of old keys to canonical keys for one table.

    A `KeyMap` maps every (possibly duplicate) old primary key value of a table
    to the canonical primary key value under which the rows describing the same
    entity are merged. Mappings may form chains (``a -> b -> c``); chains are
    resolved transitively by :meth:`resolve`, while cycles raise
    :class:`MergeError`.

    For tables with a single-column primary key, keys are plain scalars::

        KeyMap('countries.csv', {'DE': 'DEU', 'GER': 'DEU'})

    For tables with a composite primary key, keys are tuples (or lists) with one
    component per primary key column, in primary key order::

        KeyMap('names.csv', {('a', 'x'): ('a', 'y'), ('b', 'x'): ('a', 'y')})

    Key values are parsed according to the datatype declared for the
    corresponding primary key column once the `KeyMap` is bound to a
    :class:`csvw.TableGroup` (see :meth:`bind`), so e.g. an integer key supplied
    as the string ``'7'`` is normalized to ``7``.
    """
    table: str
    mapping: dict[Any, Any]
    _table: Optional[Table] = dataclasses.field(default=None, repr=False, compare=False)
    _parsed: Optional[dict[Any, Any]] = dataclasses.field(
        default=None, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.mapping, dict):
            raise MergeError(
                f'KeyMap for table {self.table!r} must be initialized with a dict')
        if not self.mapping:
            raise MergeError(f'KeyMap for table {self.table!r} must not be empty')

    #
    # Binding / type-aware parsing
    #
    def bind(self, table: Table) -> 'KeyMap':
        """
        Bind the key map to the table description it applies to and parse all
        keys according to the declared primary key column datatypes.

        :return: The `KeyMap` itself, to allow chained calls.
        """
        pk = table.tableSchema.primaryKey
        if not pk:
            raise MergeError(
                f'cannot merge table {self.table!r}: it does not declare a primaryKey')
        cols = [table.tableSchema.get_column(name, strict=True) for name in pk]
        self._table = table
        self._parsed = collections.OrderedDict()
        for old, canonical in self.mapping.items():
            old_key = self._parse_key(old, cols, pk, 'mapping key')
            new_key = self._parse_key(canonical, cols, pk, 'mapping target')
            self._check_arity(old_key, new_key)
            if old_key in self._parsed and self._parsed[old_key] != new_key:
                raise MergeError(
                    f'duplicate mapping for key {old_key!r} in table {self.table!r}: '
                    f'{self._parsed[old_key]!r} vs {new_key!r}')
            self._parsed[old_key] = new_key
        self._detect_cycles()
        return self

    @property
    def is_composite(self) -> bool:
        """Whether the key map addresses a composite (multi-column) key."""
        pk = self._table.tableSchema.primaryKey
        return bool(pk and len(pk) > 1)

    @staticmethod
    def _parse_key(value, columns, pk, what):
        arity = len(pk)
        if arity == 1:
            return KeyMap._parse_scalar(value, columns[0], pk[0], what)
        if not isinstance(value, (tuple, list)):
            raise MergeError(
                f'composite key {what} {value!r} must be a sequence of {arity} components')
        if len(value) != arity:
            raise MergeError(
                f'composite key {what} {value!r} has {len(value)} components, '
                f'expected {arity} for primary key {pk}')
        return tuple(
            KeyMap._parse_scalar(component, col, name, what)
            for component, col, name in zip(value, columns, pk))

    @staticmethod
    def _parse_scalar(value, column, colname, what):
        if value is None:
            raise MergeError(
                f'null is not a valid {what} for primary key column {colname!r}')
        datatype = column.inherit('datatype') if column is not None else None
        if datatype is not None and isinstance(value, str):
            try:
                return datatype.read(value)
            except ValueError as e:
                raise MergeError(
                    f'cannot parse {what} {value!r} according to datatype '
                    f'{datatype.base!r} of column {colname!r}') from e
        return value

    def _check_arity(self, old_key, new_key):
        # In composite mode `_parse_key` already guarantees both keys are
        # tuples of the primary key's arity; we only guard against mismatched
        # component counts defensively.
        if self.is_composite and len(new_key) != len(old_key):  # pragma: no cover
            raise MergeError(
                f'mapping target {new_key!r} must match composite key arity '
                f'({len(old_key)}) in table {self.table!r}')

    #
    # Chain resolution and cycle handling
    #
    def _detect_cycles(self):
        # Resolve every key while tracking the resolution path; revisiting a key
        # on the path means the mapping contains a cycle.
        for start in self._parsed:
            seen = []
            current = start
            while current in self._parsed:
                if current in seen:
                    chain = ' -> '.join(repr(k) for k in seen + [current])
                    raise MergeError(
                        f'cyclic key mapping in table {self.table!r}: {chain}')
                seen.append(current)
                current = self._parsed[current]

    def resolve(self, key: Any) -> Any:
        """
        Resolve a (possibly old) key to its canonical value by following the
        mapping chain until a key is reached that is not itself mapped.

        :param key: A scalar key or a tuple of composite key components.
        :return: The canonical key; keys absent from the mapping are returned \
        unchanged (identity).
        """
        current = key
        seen = []
        while current in self._parsed:
            if current in seen:  # pragma: no cover - cycles are detected at bind time
                chain = ' -> '.join(repr(k) for k in seen + [current])
                raise MergeError(f'cyclic key mapping in table {self.table!r}: {chain}')
            seen.append(current)
            current = self._parsed[current]
        return current

    #
    # Convenience helpers used while merging
    #
    def row_key(self, row: dict) -> Any:
        """Extract the (already typed) primary key value from a data row."""
        pk = self._table.tableSchema.primaryKey
        if len(pk) == 1:
            return row[pk[0]]
        return tuple(row[name] for name in pk)

    def assign(self, row: dict, key: Any) -> None:
        """Write a (canonical) key value back into a data row."""
        pk = self._table.tableSchema.primaryKey
        if len(pk) == 1:
            row[pk[0]] = key
        else:
            for name, component in zip(pk, key):
                row[name] = component


def _is_empty(value: Any) -> bool:
    """A value counts as empty if it is null or an empty (multi-)value."""
    return value is None or value == '' or value == []


def _normalize_keymaps(
        tg: TableGroup,
        keymaps: Union[dict[str, Union[KeyMap, dict]], list[KeyMap]]) -> dict[str, KeyMap]:
    """Turn the user supplied mapping specification into bound `KeyMap` objects."""
    if isinstance(keymaps, KeyMap):
        keymaps = [keymaps]
    if isinstance(keymaps, dict):
        items = [(name, km if isinstance(km, KeyMap) else KeyMap(name, km))
                 for name, km in keymaps.items()]
    else:
        items = [(km.table, km) for km in keymaps]

    bound = {}
    for name, km in items:
        if name not in tg.tabledict:
            raise MergeError(f'KeyMap references unknown table {name!r}')
        if name in bound:
            raise MergeError(f'duplicate KeyMap for table {name!r}')
        bound[name] = km.bind(tg.tabledict[name])
    return bound


def _list_fk_columns(table: Table) -> set[str]:
    """Columns of a table that act as list-valued (many-to-many) foreign keys."""
    list_valued = {c.header for c in table.tableSchema.columns if c.inherit('separator')}
    res = set()
    for fk in table.tableSchema.foreignKeys:
        if fk.reference.schemaReference:
            continue
        if len(fk.columnReference) == 1 and fk.columnReference[0] in list_valued:
            res.add(fk.columnReference[0])
    return res


def _fkey_specs(tg: TableGroup) -> list[tuple[Table, list[str], str, list[str]]]:
    """All foreign-key constraints as (source table, src cols, target table, tgt cols)."""
    specs = []
    for table in tg.tables:
        for fk in table.tableSchema.foreignKeys:
            if fk.reference.schemaReference:
                # Only references between tables of the group can be rewritten.
                continue
            target_name = fk.reference.resource.string
            if target_name not in tg.tabledict:
                raise MergeError(
                    f'foreign key in table {table.local_name!r} references unknown '
                    f'table {target_name!r}')
            specs.append((
                table,
                list(fk.columnReference),
                target_name,
                list(fk.reference.columnReference)))
    return specs


def _rewrite_foreign_keys(
        tg: TableGroup,
        data: dict[str, list[collections.OrderedDict]],
        keymaps: dict[str, KeyMap]) -> None:
    """
    Rewrite **every** declared foreign key column in every table, regardless of
    whether the owning table is itself merged. This is what makes sure indirect
    references are not missed: keys are resolved through the full mapping chain
    of the referenced table.
    """
    for source, srccols, target_name, tgtcols in _fkey_specs(tg):
        keymap = keymaps.get(target_name)
        rows = data.get(source.local_name, [])
        if keymap is None or not rows:
            continue
        target = tg.tabledict[target_name]
        target_pk = target.tableSchema.primaryKey
        list_fks = _list_fk_columns(source)

        if tgtcols != target_pk:
            # A KeyMap rewrites primary key values; a foreign key targeting
            # other columns cannot be translated and would silently dangle
            # after the referenced rows have been merged.
            raise MergeError(
                f'foreign key {srccols} in table {source.local_name!r} references '
                f'columns {tgtcols} of {target_name!r} rather than its primary key '
                f'{target_pk}; merging through non-key columns is not supported')

        if len(srccols) == 1 and srccols[0] in list_fks:
            # List-valued foreign key: rewrite each element, keeping order and
            # duplicates. None (null) and [] (no items) are kept distinct.
            col = srccols[0]
            for row in rows:
                value = row[col]
                if value is None:
                    continue
                row[col] = [None if v is None else keymap.resolve(v) for v in value]
            continue

        # Scalar or composite foreign key.
        for row in rows:
            if len(srccols) == 1:
                value = row[srccols[0]]
                if value is None:
                    continue
                row[srccols[0]] = keymap.resolve(value)
            else:
                components = tuple(row[c] for c in srccols)
                if any(c is None for c in components):
                    # A partially null composite key cannot reference anything.
                    continue
                canonical = keymap.resolve(components)
                for col, value in zip(srccols, canonical):
                    row[col] = value


def _merge_scalar_attribute(table_name, colname, old, new):
    """Conservative combination rule for non-key, non-list-fk attributes."""
    if _is_empty(old):
        return new
    if _is_empty(new):
        return old
    if old == new:
        return old
    raise MergeError(
        f'conflicting values for non-key column {colname!r} while merging rows in '
        f'table {table_name!r}: {old!r} vs {new!r}')


def _merge_table_rows(
        table: Table,
        rows: list[collections.OrderedDict],
        keymap: KeyMap) -> list[collections.OrderedDict]:
    """Merge the rows of one table, grouping them by canonical primary key."""
    pk = table.tableSchema.primaryKey
    list_fks = _list_fk_columns(table)
    merged = collections.OrderedDict()  # canonical key -> merged row
    passthrough = []  # rows without a (non-null) primary key

    for row in rows:
        key = keymap.row_key(row)
        if key is None:
            passthrough.append(row)
            continue
        canonical = keymap.resolve(key)
        rewritten = copy.deepcopy(row)
        keymap.assign(rewritten, canonical)

        if canonical not in merged:
            merged[canonical] = rewritten
            continue

        target = merged[canonical]
        for colname, value in rewritten.items():
            if colname in pk:
                # The primary key has just been canonicalized; the values must
                # now be identical by construction.
                if target[colname] != value:  # pragma: no cover
                    raise MergeError(
                        f'internal error: canonical keys differ on column {colname!r}')
                continue
            if colname in list_fks:
                # List-valued FKs accumulate: concatenate in row order,
                # preserving order and duplicates; null acts as the empty list.
                if value is None:
                    continue
                if target[colname] is None:
                    target[colname] = list(value)
                else:
                    target[colname] = list(target[colname]) + list(value)
                continue
            target[colname] = _merge_scalar_attribute(
                table.local_name, colname, target[colname], value)

    return list(merged.values()) + passthrough


def merge_tablegroup(
        tg: TableGroup,
        data: dict[str, list[collections.OrderedDict]],
        keymaps: Union[dict[str, Union[KeyMap, dict]], list[KeyMap]],
        *,
        check: bool = True) -> dict[str, list[collections.OrderedDict]]:
    """
    Merge rows of a TableGroup's data according to explicit old-key to
    canonical-key mappings.

    The merge rewrites the actual data (not just metadata):

    1. every supplied :class:`KeyMap` is bound to its table and validated
       (matching primary key, parseable keys, no mapping cycles);
    2. **all** declared foreign keys are rewritten - scalar, composite and
       list-valued ones, including foreign keys on tables that are not merged
       themselves but reference a merged table (indirect references);
    3. rows of each table with a `KeyMap` are grouped by canonical primary key
       and merged. Non-key attributes only combine when equal or one-sided
       empty (otherwise a :class:`MergeError` aborts the whole batch); entries
       of list-valued foreign-key columns are concatenated in row order,
       preserving order and duplicates.

    :param tg: The :class:`csvw.TableGroup` describing schema and relations.
    :param data: Table data as returned by :meth:`TableGroup.read`, i.e. a dict \
    mapping table URLs to lists of row dicts. The input is not modified.
    :param keymaps: Either a dict mapping table URLs to either :class:`KeyMap` \
    objects or plain mapping dicts, or a list of :class:`KeyMap` objects.
    :param check: If `True`, run :func:`check_referential_integrity` on the \
    merged data before returning.
    :return: A new dict of merged table data in the same shape as `data`.
    """
    bound = _normalize_keymaps(tg, keymaps)
    merged_data = collections.OrderedDict(
        (name, [copy.deepcopy(row) for row in rows])
        for name, rows in data.items())

    # Rewrite foreign keys first so that indirect references and list-valued
    # foreign keys already carry canonical keys when rows are combined.
    _rewrite_foreign_keys(tg, merged_data, bound)

    for name, keymap in bound.items():
        if name in merged_data:
            merged_data[name] = _merge_table_rows(
                tg.tabledict[name], merged_data[name], keymap)

    if check:
        check_referential_integrity(tg, merged_data)
    return merged_data


def check_referential_integrity(
        tg: TableGroup,
        data: dict[str, list[collections.OrderedDict]],
        *,
        strict: bool = False) -> bool:
    """
    In-memory primary/foreign-key validation for TableGroup data.

    Mirrors :meth:`TableGroup.check_referential_integrity` but operates on data
    already in memory (e.g. the result of :func:`merge_tablegroup`) instead of
    re-reading CSV files. Primary keys must be unique and non-null, and every
    non-null foreign key (each element of a list-valued foreign key included)
    must resolve to an existing primary key in the referenced table.

    :raises MergeError: If any constraint is violated.
    """
    # Collect primary key values per table and verify uniqueness / non-nullness.
    primary_keys = {}
    # Values of any (table, column reference) that foreign keys may point at;
    # CSVW allows foreign keys against columns other than the primary key.
    reference_sets = {}
    for table in tg.tables:
        name = table.local_name
        pk = table.tableSchema.primaryKey
        if not pk:
            continue
        seen = set()
        for row in data.get(name, []):
            key = tuple(row[c] for c in pk) if len(pk) > 1 else (row[pk[0]],)
            if any(component is None for component in key):
                raise MergeError(
                    f'null primary key value in table {name!r}: {key}')
            if key in seen:
                raise MergeError(f'duplicate primary key {key} in table {name!r}')
            seen.add(key)
        primary_keys[name] = (pk, seen)

    def values_for(table_name, colref):
        cache_key = (table_name, tuple(colref))
        if cache_key not in reference_sets:
            rows = data.get(table_name, [])
            if len(colref) == 1:
                reference_sets[cache_key] = {
                    (row[colref[0]],) for row in rows if row[colref[0]] is not None}
            else:
                reference_sets[cache_key] = {
                    tuple(row[c] for c in colref) for row in rows
                    if all(row[c] is not None for c in colref)}
        return reference_sets[cache_key]

    for source, srccols, target_name, tgtcols in _fkey_specs(tg):
        if target_name not in primary_keys:
            raise MergeError(
                f'referenced table {target_name!r} does not declare a primary key')
        valid_keys = values_for(target_name, tgtcols)
        list_fks = _list_fk_columns(source)
        for row in data.get(source.local_name, []):
            if len(srccols) == 1 and srccols[0] in list_fks:
                value = row[srccols[0]]
                if value is None:
                    continue
                candidates = [(v,) for v in value if v is not None]
            elif len(srccols) == 1:
                value = row[srccols[0]]
                if value is None:
                    if strict:
                        raise MergeError(
                            f'null foreign key {srccols} in table {source.local_name!r}')
                    continue
                candidates = [(value,)]
            else:
                components = tuple(row[c] for c in srccols)
                if any(c is None for c in components):
                    if strict:
                        raise MergeError(
                            f'null foreign key {srccols} in table {source.local_name!r}')
                    continue
                candidates = [components]
            for candidate in candidates:
                if candidate not in valid_keys:
                    raise MergeError(
                        f'foreign key {candidate} in table {source.local_name!r} '
                        f'not found in table {target_name!r}')
    return True
