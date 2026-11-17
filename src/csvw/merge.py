"""
Multi-source master data merging for a CSVW table.

Several sources may contribute fields to the same set of entities. This module merges the
current snapshots of those sources into one master table (a CSVW described CSV file),
rebuilding the master **from the snapshots** on every run.

The merge is driven by three pieces of information:

1. A **business key**: one or more columns identifying the same entity across sources
   (usually the master table's `primaryKey`).
2. Per source, per field **priorities** (`int`, lower numbers win) and an
   **`null_overrides`** flag declaring whether a null value contributed by the source
   may overwrite values supplied by lower-priority sources.
3. The **complete current snapshot** of every source, persisted between runs. This makes
   deletions observable: when a source deletes a record or withdraws a field, the value
   contributed by other still-valid sources is restored instead of keeping the previous
   merge result forever.

For every business key and every field the valid contributions are selected as follows:

- Null contributions are ignored unless the contributing source has `null_overrides`
  enabled (and then they only win if no higher-priority non-null contribution exists).
- Among the remaining contributions the one from the source with the lowest priority
  number wins.
- If several sources at the *same* winning priority provide distinct non-null values a
  :class:`MergeConflict` is raised - the merge cannot silently pick one.

Every run produces one atomic **round**. The typed master table, the field-level
provenance records and the SQLite database always describe the same round; if merging
fails (e.g. because of a conflict), none of the published outputs change.
"""
import os
import json
import shutil
import sqlite3
import tempfile
import dataclasses
import collections
from typing import Optional, Union, Any, Iterable
from collections.abc import Sequence
from pathlib import Path

from .metadata import TableGroup, Table, Column
from .db import Database


__all__ = [
    'Source', 'MergeConflict', 'FieldProvenance', 'MergeResult', 'MasterMerger',
    'merge_sources',
]


class MergeConflict(ValueError):
    """
    Raised when sources at the same priority contribute different non-null values for the
    same entity and field.

    All conflicts of a round are collected, so that a single run reports every problem
    rather than failing on the first one.
    """

    def __init__(self, conflicts: list['ConflictDetail']):
        self.conflicts = conflicts
        lines = [
            f'conflicting values for {c.key_fields}={c.key!r}, field {c.field!r} '
            f'at priority {c.priority}: ' + ' | '.join(
                f'{s}={v!r}' for s, v in sorted(c.values.items()))
            for c in conflicts
        ]
        super().__init__('; '.join(lines))


@dataclasses.dataclass
class ConflictDetail:
    """A single same-priority, different-value conflict."""
    table: str
    key_fields: tuple[str, ...]
    key: tuple
    field: str
    priority: int
    values: dict[str, Any]


@dataclasses.dataclass
class Source:
    """
    Declaration of one contributing source.

    :ivar name: Unique identifier of the source, used in provenance records and snapshot
        file names.
    :ivar path: Path (or URL) of a CSV file described by the master table's schema. If
        `None`, rows must be supplied directly via the `rows` argument of
        :meth:`MasterMerger.run`.
    :ivar priorities: Mapping of field (column header) to priority number. Lower numbers
        mean higher priority. Fields not listed get `default_priority`.
    :ivar null_overrides: Per-source flag (or a set/tuple of field names) declaring
        whether null values of the source overwrite values from lower-priority sources.
        A plain `bool` sets the policy for every field; an iterable enables it only for
        the listed fields.
    :ivar default_priority: Priority assigned to fields not present in `priorities`.
    """
    name: str
    path: Optional[Union[str, Path]] = None
    priorities: Optional[dict[str, int]] = None
    null_overrides: Union[bool, Iterable[str]] = False
    default_priority: int = 100

    def __post_init__(self):
        self.priorities = dict(self.priorities or {})
        if isinstance(self.null_overrides, bool):
            self._null_fields = None
        else:
            # Keep the field-level policy; the bool attribute just mirrors whether the
            # list is non-empty for convenient introspection.
            self._null_fields = set(self.null_overrides)
            self.null_overrides = bool(self._null_fields)

    def priority_for(self, field: str) -> int:
        """:return: The priority this source declares for `field`."""
        return self.priorities.get(field, self.default_priority)

    def null_overrides_for(self, field: str) -> bool:
        """:return: Whether a null in `field` of this source overwrites other sources."""
        if self._null_fields is None:
            return self.null_overrides
        return field in self._null_fields

    def asdict(self) -> dict:
        """JSON-serialisable description, used for the persisted merger state."""
        return {
            'name': self.name,
            'path': str(self.path) if self.path is not None else None,
            'priorities': self.priorities,
            'null_overrides': True if self._null_fields is None
            else sorted(self._null_fields),
            'default_priority': self.default_priority,
        }

    @classmethod
    def fromdict(cls, d: dict) -> 'Source':
        return cls(**d)


@dataclasses.dataclass
class FieldProvenance:
    """Records which source contributed the value of one entity's field in a round."""
    key: tuple
    field: str
    source: str
    priority: int
    round_id: str

    def asrow(self, key_fields: Sequence[str]) -> collections.OrderedDict:
        row = collections.OrderedDict(zip(key_fields, self.key))
        row['field'] = self.field
        row['source'] = self.source
        row['priority'] = self.priority
        row['round'] = self.round_id
        return row


@dataclasses.dataclass
class MergeResult:
    """Outcome of a merge round."""
    round_id: str
    key_fields: tuple[str, ...]
    master: list[collections.OrderedDict]
    provenance: list[FieldProvenance]
    snapshots: dict[str, list[dict]]

    @property
    def keys(self) -> list[tuple]:
        """:return: The business keys present in the master data, in first-seen order."""
        return [tuple(r[k] for k in self.key_fields) for r in self.master]


class MasterMerger:
    """
    Merge the current snapshots of several sources into the master data of one CSVW table.

    A merger is bound to a working directory holding the durable state:

    - ``state.json`` - round id, business key and source declarations,
    - ``snapshots/<source>.json`` - the complete current snapshot of each source,
    - ``master.csv`` / ``master-metadata.json`` - the typed master table (a CSVW
      TableGroup, together with the ``field-sources.csv`` provenance table),
    - ``master.sqlite`` - the same round's data loaded into SQLite.

    All outputs of a run are staged in a temporary directory and moved into place only
    after the whole round - including conflict checks and the SQLite load - succeeds.
    Thus a failed run never leaves a half-finished update behind.

    :param table: The CSVW :class:`~csvw.Table` whose schema describes master and source
        data. Its schema must declare a (possibly composite) `primaryKey` which serves as
        business key.
    :param workdir: Directory for snapshots and published outputs.
    :param sources: Sources participating in the merge.
    :param round_id: Explicit identifier for the next round. If `None`, an id derived
        from the current UTC time is generated.
    """

    provenance_table = 'field-sources.csv'

    def __init__(
            self,
            table: Table,
            workdir: Union[str, Path],
            sources: Sequence[Source],
            round_id: Optional[str] = None,
    ):
        if not sources:
            raise ValueError('at least one source must be declared')
        names = [s.name for s in sources]
        if len(set(names)) != len(names):
            raise ValueError('source names must be unique')
        if not table.tableSchema.primaryKey:
            raise ValueError('the table schema must declare a primaryKey as business key')
        self.table = table
        self.key_fields = tuple(table.tableSchema.primaryKey)
        self.workdir = Path(workdir)
        self.sources = list(sources)
        self.snapdir = self.workdir / 'snapshots'
        self.state_path = self.workdir / 'state.json'
        self.round_id = round_id or self._new_round_id()
        self._validate_fields()

    # -- configuration -------------------------------------------------------

    def _validate_fields(self):
        headers = {c.header for c in self.table.tableSchema.columns if not c.virtual}
        unknown = set(self.key_fields) - headers
        if unknown:
            raise ValueError(f'business key fields not in schema: {sorted(unknown)}')
        for source in self.sources:
            bad = set(source.priorities) - headers
            if bad:
                raise ValueError(
                    f'source {source.name!r} declares priorities for unknown fields: '
                    f'{sorted(bad)}')
            if source._null_fields is not None:
                bad = source._null_fields - headers
                if bad:
                    raise ValueError(
                        f'source {source.name!r} declares null overrides for unknown '
                        f'fields: {sorted(bad)}')

    @staticmethod
    def _new_round_id() -> str:
        # Imported lazily so the module stays importable in restricted environments.
        import datetime  # pylint: disable=import-outside-toplevel
        return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')

    @property
    def value_fields(self) -> list[str]:
        """The non-virtual schema columns which are not part of the business key."""
        return [c.header for c in self.table.tableSchema.columns
                if not c.virtual and c.header not in self.key_fields]

    # -- snapshots -----------------------------------------------------------

    def _read_source_rows(self, source: Source, rows: Optional[Iterable[dict]]) -> list[dict]:
        if rows is not None:
            return [self._normalise_row(r, typed=True) for r in rows]
        if source.path is None:
            if (self.snapdir / f'{source.name}.json').exists():
                return self._load_snapshot(source.name)
            raise ValueError(
                f'source {source.name!r} has neither a path nor inline rows and no '
                f'previous snapshot exists')
        # Rows read through the table's schema are already typed and only contain schema
        # columns; we still normalise them to validate the business key.
        return [self._normalise_row(r, typed=False)
                for r in self.table.iterdicts(fname=source.path)]

    def _normalise_row(self, row: dict, typed: bool) -> dict:
        """
        Restrict a row to schema columns, validating the business key.

        Fields missing from `row` stay absent (a withdrawn field), while explicit nulls
        are kept as null values. Inline rows may carry raw strings, which are converted
        to typed values using the master table's column specifications.
        """
        res = collections.OrderedDict()
        for col in self.table.tableSchema.columns:
            if col.virtual or col.header not in row:
                continue
            value = row[col.header]
            if typed and value is not None and isinstance(value, str):
                value = col.read(value)
            res[col.header] = value
        key = tuple(res.get(k) for k in self.key_fields)
        if any(v is None for v in key):
            raise ValueError(
                f'row is missing business key value(s) for {self.key_fields}: {dict(row)}')
        # Business key columns are part of every row even when omitted from the input
        # ordering, because they identify the entity rather than contributing a field.
        for key_field in self.key_fields:
            res.setdefault(key_field, row[key_field])
        return res

    def _load_snapshot(self, name: str) -> list[dict]:
        """Read a persisted snapshot back, turning stored strings into typed values."""
        data = json.loads((self.snapdir / f'{name}.json').read_text(encoding='utf8'))
        columns = self.table.tableSchema.columndict
        rows = []
        for item in data['rows']:
            row = collections.OrderedDict()
            for col in self.table.tableSchema.columns:
                if col.virtual or col.header not in item:
                    continue
                raw = item[col.header]
                row[col.header] = None if raw is None else columns[col.header].read(raw)
            rows.append(row)
        return rows

    def _snapshot_payload(self, rows: list[dict]) -> dict:
        """Serialise typed values to stable strings using the column formatters."""
        columns = self.table.tableSchema.columndict
        payload_rows = []
        for row in rows:
            item = collections.OrderedDict()
            for col in self.table.tableSchema.columns:
                if col.virtual or col.header not in row:
                    continue
                value = row[col.header]
                if value is None:
                    item[col.header] = None
                else:
                    item[col.header] = columns[col.header].write(value)
            payload_rows.append(item)
        return {'rows': payload_rows}

    # -- merging -------------------------------------------------------------

    def _merge(
            self, snapshots: dict[str, list[dict]]
    ) -> tuple[list[collections.OrderedDict], list[FieldProvenance]]:
        # Collect, per business key, every non-missing contribution per field. A field
        # absent from a source's snapshot means "withdrawn" and contributes nothing.
        contributions = collections.OrderedDict()
        for source in self.sources:
            seen = set()
            for row in snapshots[source.name]:
                key = tuple(row[k] for k in self.key_fields)
                if key in seen:
                    raise ValueError(
                        f'duplicate business key {key!r} in source {source.name!r}')
                seen.add(key)
                bucket = contributions.setdefault(
                    key, collections.OrderedDict(
                        (f, []) for f in self.value_fields))
                for field in self.value_fields:
                    if field not in row:
                        continue  # the source withdrew the field
                    bucket[field].append((source, row[field]))

        # Detect every same-priority conflict before computing any output, so that a
        # failing round never partially changes the published master data.
        conflicts = []
        for key, fields in contributions.items():
            for field, contribs in fields.items():
                conflicts.extend(self._conflicts_for(key, field, contribs))
        if conflicts:
            raise MergeConflict(conflicts)

        provenance, master = [], []
        for key, fields in contributions.items():
            merged = collections.OrderedDict(zip(self.key_fields, key))
            for field in self.value_fields:
                value, winner = self._select_field(field, fields[field])
                if winner is not None:
                    merged[field] = value
                    provenance.append(FieldProvenance(
                        key=key, field=field, source=winner.name,
                        priority=winner.priority_for(field), round_id=self.round_id))
                else:
                    merged[field] = None
            master.append(merged)
        return master, provenance

    def _select_field(
            self, field: str, contribs: list[tuple[Source, Any]]
    ) -> tuple[Any, Optional[Source]]:
        """Pick the winning (value, source) pair for one field of one entity."""
        overriding_nulls = [(s, v) for s, v in contribs if v is None
                            and s.null_overrides_for(field)]
        non_null = [(s, v) for s, v in contribs if v is not None]

        if overriding_nulls:
            # An overriding null clears the field whenever no *higher or equal*
            # priority non-null contribution exists.
            null_priority = min(s.priority_for(field) for s, _ in overriding_nulls)
            better = [(s, v) for s, v in non_null
                      if s.priority_for(field) <= null_priority]
            if not better:
                top = [s for s, _ in overriding_nulls
                       if s.priority_for(field) == null_priority]
                return None, top[0]
            non_null = better

        if non_null:
            # The lowest priority number among the remaining non-null values decides.
            top_priority = min(s.priority_for(field) for s, _ in non_null)
            top = [(s, v) for s, v in non_null
                   if s.priority_for(field) == top_priority]
            # Distinct values at the same priority are reported by `_conflicts_for`;
            # with equal values any contributor may be reported as origin.
            value = top[0][1]
            winner = next(s for s, v in top if v == value)
            return value, winner
        # Only plain, non-overriding nulls: the field has no valid contribution.
        return None, None

    def _conflicts_for(
            self, key: tuple, field: str, contribs: list[tuple[Source, Any]]
    ) -> list[ConflictDetail]:
        overriding_nulls = [(s, v) for s, v in contribs if v is None
                            and s.null_overrides_for(field)]
        non_null = [(s, v) for s, v in contribs if v is not None]
        if overriding_nulls:
            # A winning overriding null removes the lower-priority non-null values from
            # the conflict check.
            null_priority = min(s.priority_for(field) for s, _ in overriding_nulls)
            non_null = [(s, v) for s, v in non_null
                        if s.priority_for(field) <= null_priority]
        if not non_null:
            return []
        top_priority = min(s.priority_for(field) for s, _ in non_null)
        values = collections.OrderedDict(
            (s.name, v) for s, v in non_null
            if s.priority_for(field) == top_priority)
        if len({self._hashable(v) for v in values.values()}) > 1:
            return [ConflictDetail(
                table=self.table.local_name, key_fields=self.key_fields, key=key,
                field=field, priority=top_priority, values=dict(values))]
        return []

    @staticmethod
    def _hashable(value: Any):
        """Turn list values into hashable tuples so distinctness can be compared."""
        if isinstance(value, list):
            return tuple(MasterMerger._hashable(v) for v in value)
        if isinstance(value, dict):
            return tuple(sorted(
                (k, MasterMerger._hashable(v)) for k, v in value.items()))
        return value

    # -- run / publishing ----------------------------------------------------

    def run(self, rows: Optional[dict[str, Iterable[dict]]] = None) -> MergeResult:
        """
        Execute one merge round and publish its outputs atomically.

        :param rows: Optional mapping of source name to iterable of rows. Rows supplied
            here take precedence over reading the source's `path`; sources not mentioned
            are read from their path or, failing that, keep their previous snapshot.
        :return: A :class:`MergeResult` describing the new round.
        :raises MergeConflict: If same-priority sources provide different non-null
            values; in that case all previously published outputs remain untouched.
        """
        rows = rows or {}
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.snapdir.mkdir(exist_ok=True)

        # 1. Refresh the complete snapshot of every participating source.
        snapshots = collections.OrderedDict()
        for source in self.sources:
            snapshots[source.name] = self._read_source_rows(source, rows.get(source.name))

        # 2. Recompute the master and provenance purely from the current snapshots.
        master, provenance = self._merge(snapshots)
        result = MergeResult(
            round_id=self.round_id, key_fields=self.key_fields, master=master,
            provenance=provenance, snapshots=snapshots)

        # 3. Stage every output in a private temp directory ...
        stage = Path(tempfile.mkdtemp(prefix='csvw-merge-', dir=str(self.workdir)))
        try:
            self._write_snapshots(stage, snapshots)
            self._write_state(stage)
            output_tg = self._write_outputs(stage, master, provenance)
            self._write_sqlite(stage, output_tg)
            # 4. ... and only move them into place once the whole round succeeded.
            self._publish(stage)
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise
        shutil.rmtree(stage, ignore_errors=True)
        return result

    def _write_snapshots(self, stage: Path, snapshots: dict[str, list[dict]]):
        snap_stage = stage / 'snapshots'
        snap_stage.mkdir()
        for name, rows in snapshots.items():
            (snap_stage / f'{name}.json').write_text(
                json.dumps(self._snapshot_payload(rows), indent=2, ensure_ascii=False),
                encoding='utf8')

    def _write_state(self, stage: Path):
        state = {
            'round': self.round_id,
            'table': self.table.local_name,
            'business_key': list(self.key_fields),
            'sources': [s.asdict() for s in self.sources],
        }
        (stage / 'state.json').write_text(
            json.dumps(state, indent=2, ensure_ascii=False), encoding='utf8')

    def _output_tablegroup(
            self, master_rows: list[dict],
            provenance_rows: list[FieldProvenance]) -> TableGroup:
        """Build the published TableGroup: master table plus provenance table."""
        master_table = Table.fromvalue({
            'url': 'master.csv',
            'tableSchema': {
                'columns': [c.asdict() for c in self.table.tableSchema.columns
                            if not c.virtual],
                'primaryKey': list(self.key_fields),
            },
        })
        prov_columns = [
            Column.fromvalue({'name': f, 'datatype': self._key_datatype(f)})
            for f in self.key_fields
        ]
        prov_columns += [
            Column.fromvalue({'name': 'field'}),
            Column.fromvalue({'name': 'source'}),
            Column.fromvalue({'name': 'priority', 'datatype': 'integer'}),
            Column.fromvalue({'name': 'round'}),
        ]
        provenance_table = Table.fromvalue({
            'url': self.provenance_table,
            'tableSchema': {
                'columns': [c.asdict(omit_defaults=False) for c in prov_columns],
                'primaryKey': list(self.key_fields) + ['field'],
            },
        })
        tg = TableGroup.fromvalue({'tables': [
            master_table.asdict(omit_defaults=False),
            provenance_table.asdict(omit_defaults=False),
        ]})
        tg._master_rows = master_rows  # type: ignore[attr-defined]
        tg._provenance_rows = [  # type: ignore[attr-defined]
            p.asrow(self.key_fields) for p in provenance_rows]
        return tg

    def _key_datatype(self, field: str) -> Optional[str]:
        col = self.table.tableSchema.columndict[field]
        datatype = col.inherit('datatype')
        return datatype.base if datatype else None

    def _write_outputs(
            self, stage: Path, master_rows: list[dict],
            provenance_rows: list[FieldProvenance]) -> TableGroup:
        tg = self._output_tablegroup(master_rows, provenance_rows)
        tg.tabledict['master.csv'].write(
            tg._master_rows, fname=stage / 'master.csv')  # pylint: disable=protected-access
        tg.tabledict[self.provenance_table].write(
            tg._provenance_rows,  # pylint: disable=protected-access
            fname=stage / self.provenance_table)
        tg.to_file(stage / 'master-metadata.json', omit_defaults=False)
        return tg

    def _write_sqlite(self, stage: Path, tg: TableGroup):
        db_path = stage / 'master.sqlite'
        db = Database(tg, fname=db_path)
        db.write(
            force=False,
            **{'master.csv': tg._master_rows,  # pylint: disable=protected-access
               self.provenance_table: tg._provenance_rows})  # pylint: disable=protected-access
        # Same-round sanity check: the DB must contain exactly the rows of this round,
        # and every provenance row must carry the round id of this run.
        with sqlite3.connect(str(db_path)) as conn:
            master_count = conn.execute('SELECT count(*) FROM `master.csv`').fetchone()[0]
            prov_count = conn.execute(
                f'SELECT count(*) FROM `{self.provenance_table}`').fetchone()[0]
            foreign_rounds = [r[0] for r in conn.execute(
                f'SELECT DISTINCT `round` FROM `{self.provenance_table}`')]
        if master_count != len(tg._master_rows):  # pylint: disable=protected-access
            raise RuntimeError('SQLite round mismatch while writing master data')
        if prov_count != len(tg._provenance_rows):  # pylint: disable=protected-access
            raise RuntimeError('SQLite round mismatch while writing provenance data')
        if foreign_rounds and foreign_rounds != [self.round_id]:
            raise RuntimeError('SQLite contains provenance of a different merge round')

    def query(self, sql: str, params: Sequence = ()) -> list[tuple]:
        """
        Run a read-only query against the SQLite database of the published round.

        The database is only replaced atomically after a successful merge round, so query
        results always correspond to the typed master table and provenance records on
        disk.
        """
        with sqlite3.connect(str(self.workdir / 'master.sqlite')) as conn:
            return list(conn.execute(sql, params))

    def _publish(self, stage: Path):
        """Move staged files to their final locations, replacing previous rounds."""
        def replace(src: Path, dest: Path):
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)

        replace(stage / 'state.json', self.state_path)
        # Snapshots of sources that no longer participate must not linger.
        fresh = {f'{s.name}.json' for s in self.sources}
        if self.snapdir.exists():
            for old in self.snapdir.glob('*.json'):
                if old.name not in fresh:
                    old.unlink()
        for src in (stage / 'snapshots').glob('*.json'):
            replace(src, self.snapdir / src.name)
        for name in ['master.csv', 'master-metadata.json', self.provenance_table,
                     'master.sqlite']:
            replace(stage / name, self.workdir / name)

    # -- introspection -------------------------------------------------------

    @classmethod
    def from_state(cls, workdir: Union[str, Path],
                   round_id: Optional[str] = None) -> 'MasterMerger':
        """Re-instantiate a merger from its persisted ``state.json``."""
        state = json.loads(Path(workdir, 'state.json').read_text(encoding='utf8'))
        metadata = TableGroup.from_file(Path(workdir, 'master-metadata.json'))
        table = metadata.tabledict['master.csv']
        sources = [Source.fromdict(d) for d in state['sources']]
        return cls(table, workdir, sources, round_id=round_id)

    def current_master(self) -> list[collections.OrderedDict]:
        """Read back the master data of the last successfully published round."""
        metadata = TableGroup.from_file(self.workdir / 'master-metadata.json')
        return list(metadata.tabledict['master.csv'].iterdicts(
            fname=self.workdir / 'master.csv'))


def merge_sources(
        table: Table,
        workdir: Union[str, Path],
        sources: Sequence[Source],
        rows: Optional[dict[str, Iterable[dict]]] = None,
        round_id: Optional[str] = None) -> MergeResult:
    """
    Convenience wrapper running a single :class:`MasterMerger` round.

    .. code-block:: python

        >>> from csvw.merge import Source, merge_sources
        >>> result = merge_sources(table, 'out', [
        ...     Source('crm', priorities={'name': 1}, null_overrides=True),
        ...     Source('erp', priorities={'name': 2, 'tax_id': 1}),
        ... ])  # doctest: +SKIP
    """
    return MasterMerger(table, workdir, sources, round_id=round_id).run(rows=rows)
