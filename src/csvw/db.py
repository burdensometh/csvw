"""
SQLite as alternative storage backend for a TableGroup's data.

For the most part, translation of a TableGroup's tableSchema to SQL works as expected:

- each table is converted to a `CREATE TABLE` statement
- each column specifies a column in the corresponding `CREATE TABLE` statement
- `foreignKey` constraints are added according to the corresponding `tableSchema` property.

List-valued foreignKeys are supported as follows: For each pair of tables related through a
list-valued foreign key, an association table is created. To make it possible to distinguish
multiple list-valued foreign keys between the same two tables, the association table has
a column `context`, which stores the name of the foreign key column from which a row in the
assocation table was created.

Other list-valued columns work in two different ways: If the atomic datatype is `string`, the
specified separator is used to create a concatenated string representation in the database field.
Otherwise, the list of values is serialized as JSON.

SQL table and column names can be customized by passing a translator callable when instantiating
a :class:`Database`.

**Incremental changes**

A :class:`Database` can also receive incremental changes for existing data, rather than being
rebuilt from scratch.  Changes are applied in batches via :meth:`Database.apply_changes`.  Each
change specifies the table, the (possibly composite) primary key of the affected row, a
monotonically increasing version and the complete new values of the row.  Omitting the values
(or setting ``delete=True``) marks the row as deleted.

Versions are tracked per table and key in an internal table, so applying a change is idempotent
as long as the same version is not reused for different values.  Changes with a version older
than the recorded one are ignored, and a repeated version with identical values is a no-op.  A
batch is applied atomically: either all changes take effect, or neither the data nor the recorded
version progress is modified.

Within a batch, rows may refer to parent rows which are added later in the same batch.  Foreign
key constraints are checked against the state at the end of the batch.  Consequently, deleting a
parent row which is still referenced is rejected, while deleting a parent together with all
referencing rows succeeds.

SQLite support has the following limitations:

- regex constraints on strings (as specified via a :class:`csvw.Datatype`'s format attribute) are
  not enforced by the database.
"""
import json
import datetime
from typing import Optional, Union, Protocol, Callable, Any
import decimal
import pathlib
import sqlite3
import functools
import contextlib
import collections
from collections.abc import Sequence, Iterator, Iterable, Mapping
import dataclasses

import csvw
from csvw.datatypes import DATATYPES
from csvw.metadata import TableGroup, Datatype
from .utils import optcast


def identity(s):  # pylint: disable=C0116
    return s


@dataclasses.dataclass
class DBType:
    """A DB datatype together with read/write converters."""
    name: str
    convert: Callable[[Any], Any] = identity
    read: Callable[[Any], Any] = identity


TYPE_MAP = {
    'string': DBType('TEXT'),
    'integer': DBType('INTEGER'),
    'boolean': DBType('INTEGER', optcast(int), optcast(bool)),
    'decimal': DBType('REAL', optcast(float), optcast(decimal.Decimal)),
    'hexBinary': DBType('BLOB'),
}


class SchemaTranslator(Protocol):  # pylint: disable=R0903,C0115
    def __call__(self, table: str, column: Optional[str] = None) -> str:
        ...  # pragma: no cover


class ColumnTranslator(Protocol):  # pylint: disable=R0903,C0115
    def __call__(self, column: str) -> str:
        ...  # pragma: no cover


def quoted(*names: str) -> str:
    """Returns a comma-separated list of quoted schema object names."""
    return ','.join(f'`{name}`' for name in names)


def insert(db: sqlite3.Connection,
           translate: SchemaTranslator,
           table: str,
           keys: Sequence[str],
           *rows: list,
           single: Optional[bool] = False):
    """
    Insert a sequence of rows into a table.

    :param db: Database connection.
    :param translate: Callable translating table and column names to proper schema object names.
    :param table: Untranslated table name.
    :param keys: Untranslated column names.
    :param rows: Sequence of rows to insert.
    :param single: Flag signaling whether to insert all rows at once using `executemany` or one at \
    a time, allowing for more focused debugging output in case of errors.
    """
    if rows:
        cols = quoted(*[translate(table, k) for k in keys])
        vals = ','.join(['?' for _ in keys])
        sql = f"INSERT INTO {quoted(translate(table))} ({cols}) VALUES ({vals})"
        try:
            db.executemany(sql, rows)
        except:  # noqa: E722 - this is purely for debugging.  pylint: disable=bare-except
            if not single:
                for row in rows:
                    insert(db, translate, table, keys, row, single=True)
            else:
                print(sql)
                print(rows)
                raise


def select(db: sqlite3.Connection, table: str) -> tuple[list[str], Sequence]:
    """Shortcut to construct and execute simple SELECT statements."""
    cu = db.execute(f"SELECT * FROM {quoted(table)}")
    cols = [d[0] for d in cu.description]
    return cols, list(cu.fetchall())


@dataclasses.dataclass
class ColSpec:
    """
    A `ColSpec` captures sufficient information about a :class:`csvw.Column` for the DB schema.
    """
    name: str
    csvw_type: str = 'string'
    separator: str = None
    db_type: DBType = None
    required: bool = False
    csvw: Datatype = None

    def __post_init__(self):
        self.csvw_type = self.csvw_type or 'string'
        if self.csvw_type in TYPE_MAP:
            self.db_type = TYPE_MAP[self.csvw_type]
        else:
            self.db_type = DBType(
                'TEXT', DATATYPES[self.csvw_type].to_string, DATATYPES[self.csvw_type].to_python)
        if self.separator and self.db_type != 'TEXT':
            self.db_type = DBType('TEXT', self.db_type.convert, self.db_type.read)

    def check(self, translate: ColumnTranslator) -> Optional[str]:
        """
        We try to convert as many data constraints as possible into SQLite CHECK constraints.

        :param translate: Callable to translate column names between CSVW metadata and DB schema.
        :return: A string suitable as argument of an SQL CHECK constraint.
        """
        if not self.csvw:
            return None
        c, cname = self.csvw, translate(self.name)
        constraints = []
        if (c.minimum is not None) or (c.maximum is not None):
            func = {
                'date': 'date',
                'datetime': 'datetime',
            }.get(self.csvw_type)
            if c.minimum is not None:
                if func:
                    constraints.append(f"{func}(`{cname}`) >= {func}('{c.minimum}')")
                else:
                    constraints.append(f'`{cname}` >= {c.minimum}')
            if c.maximum is not None:
                if func:
                    constraints.append(f"{func}(`{cname}`) <= {func}('{c.maximum}')")
                else:
                    constraints.append(f'`{cname}` <= {c.maximum}')
        elif any(cc is not None for cc in [c.length, c.minLength, c.maxLength]):
            if c.length:
                constraints.append(f'length(`{cname}`) = {c.length}')
            if c.minLength:
                constraints.append(f'length(`{cname}`) >= {c.minLength}')
            if c.maxLength:
                constraints.append(f'length(`{cname}`) <= {c.maxLength}')
        return ' AND '.join(constraints)

    def sql(self, translate: ColumnTranslator) -> str:
        """Format the column metadata suitable for inclusion in a CREATE TABLE statement."""
        _check = self.check(translate)
        null_constraint = ' NOT NULL' if self.required else ''
        check_constraint = f' CHECK ({_check})' if _check else ''
        return f'`{translate(self.name)}` {self.db_type.name}{null_constraint}{check_constraint}'


@dataclasses.dataclass
class TableSpec:
    """
    A `TableSpec` captures sufficient information about a :class:`csvw.Table` for the DB schema.

    .. note::

        We support "light-weight" many-to-many relationships by allowing list-valued foreign key
        columns in CSVW. In the database these columns are turned into an associative table, adding
        the name of the column as value a `context` column. Thus, multiple columns in a table my be
        specified as targets of many-to-many relations with the same table.

        .. seealso:: `<https://en.wikipedia.org/wiki/Associative_entity>`_
    """
    name: str
    columns: list[ColSpec] = dataclasses.field(default_factory=list)
    foreign_keys: list = dataclasses.field(default_factory=list)
    many_to_many: collections.OrderedDict = dataclasses.field(
        default_factory=collections.OrderedDict)
    primary_key: Optional[list[str]] = None

    @classmethod
    def from_table_metadata(cls,
                            table: csvw.Table,
                            drop_self_referential_fks: Optional[bool] = True) -> 'TableSpec':
        """
        Create a `TableSpec` from the schema description of a `csvw.metadata.Table`.

        :param table: `csvw.metadata.Table` instance.
        :param drop_self_referential_fks: Flag signaling whether to drop self-referential foreign \
        keys. This may be necessary, if the order of rows in a CSVW table does not guarantee \
        referential integrity when inserted in order (e.g. an eralier row refering to a later one).
        :return: `TableSpec` instance.
        """
        spec = cls(name=table.local_name, primary_key=table.tableSchema.primaryKey)
        list_valued = {c.header for c in table.tableSchema.columns if c.separator}
        for fk in table.tableSchema.foreignKeys:
            # We only support Foreign Key references between tables!
            if not fk.reference.schemaReference:
                if len(fk.columnReference) == 1 and fk.columnReference[0] in list_valued:
                    # List-valued foreign keys are turned into a many-to-many relation!
                    assert len(fk.reference.columnReference) == 1, \
                        (f'Composite key {fk.reference.columnReference} in table '
                         f'{fk.reference.resource} referenced')
                    assert spec.primary_key and len(spec.primary_key) == 1, \
                        (f'Table {spec.name} referenced by list-valued foreign key must have '
                         f'non-composite primary key')
                    spec.many_to_many[fk.columnReference[0]] = TableSpec.association_table(
                        spec.name,
                        spec.primary_key[0],
                        fk.reference.resource.string,
                        fk.reference.columnReference[0],
                    )
                elif not (drop_self_referential_fks and fk.reference.resource.string == spec.name):
                    spec.foreign_keys.append((
                        list(fk.columnReference),
                        fk.reference.resource.string,
                        list(fk.reference.columnReference),
                    ))
        for c in table.tableSchema.columns:
            if c.header not in spec.many_to_many:
                datatype = c.inherit('datatype')
                spec.columns.append(ColSpec(
                    name=c.header,
                    csvw_type=datatype.base if datatype else datatype,
                    separator=c.inherit('separator'),
                    required=c.inherit('required'),
                    csvw=c.inherit('datatype'),
                ))
        return spec

    @classmethod
    def association_table(cls, atable, apk, btable, bpk) -> 'TableSpec':
        """
        List-valued foreignKeys are supported as follows: For each pair of tables related through a
        list-valued foreign key, an association table is created. To make it possible to distinguish
        multiple list-valued foreign keys between the same two tables, the association table has
        a column `context`, which stores the name of the foreign key column from which a row in the
        assocation table was created.
        """
        afk = ColSpec(f'{atable}_{apk}')
        bfk = ColSpec(f'{btable}_{bpk}')
        if afk.name == bfk.name:
            afk.name += '_1'
            bfk.name += '_2'
        return cls(
            name=f'{atable}_{btable}',
            columns=[afk, bfk, ColSpec('context')],
            foreign_keys=[
                ([afk.name], atable, [apk]),
                ([bfk.name], btable, [bpk]),
            ]
        )

    def sql(self, translate: SchemaTranslator) -> str:
        """
        :param translate:
        :return: The SQL statement to create the table.
        """
        col_translate = functools.partial(translate, self.name)
        # Assemble the column specifications:
        clauses = [col.sql(col_translate) for col in self.columns]
        # Then add the constraints:
        if self.primary_key:
            qcols = quoted(*[col_translate(c) for c in self.primary_key])
            clauses.append(f'PRIMARY KEY({qcols})')
        for fk, ref, refcols in self.foreign_keys:
            fkcols = quoted(*[col_translate(c) for c in fk])
            rtable = quoted(translate(ref))
            pkcols = quoted(*[translate(ref, c) for c in refcols])
            clauses.append(f'FOREIGN KEY({fkcols}) REFERENCES {rtable}({pkcols}) ON DELETE CASCADE')

        clauses = ',\n    '.join(clauses)
        return '\n'.join([
            f"CREATE TABLE IF NOT EXISTS `{translate(self.name)}` (", f"{clauses}", ")"])


def schema(tg: csvw.TableGroup,
           drop_self_referential_fks: Optional[bool] = True) -> list[TableSpec]:
    """
    Convert the table and column descriptions of a `TableGroup` into specifications for the
    DB schema.

    :param tg: CSVW TableGroup.
    :param drop_self_referential_fks: Flag signaling whether to drop self-referential foreign \
    keys. This may be necessary, if the order of rows in a CSVW table does not guarantee \
    referential integrity when inserted in order (e.g. an eralier row refering to a later one).
    :return: A pair (tables, reference_tables).
    """
    tables = {}
    for table in tg.tabledict.values():
        t = TableSpec.from_table_metadata(
            table, drop_self_referential_fks=drop_self_referential_fks)
        tables[t.name] = t
        for at in t.many_to_many.values():
            tables[at.name] = at

    # We must determine the order in which tables must be created!
    ordered = collections.OrderedDict()

    # We loop through the tables repeatedly, and whenever we find one, which has all
    # referenced tables already in ordered, we move it from tables to ordered.
    while tables:
        for table in list(tables.keys()):
            if all((ref[1] in ordered) or ref[1] == table for ref in tables[table].foreign_keys):
                # All referenced tables are already created (or self-referential).
                ordered[table] = tables.pop(table)
                break
        else:
            break
    if tables:  # pragma: no cover
        raise ValueError('there seem to be cyclic dependencies between the tables')

    return list(ordered.values())


#: Name of the table in which incremental changes record their version progress.
VERSIONS_TABLE = '_csvw_change_versions'


class ChangeError(ValueError):
    """Raised if a batch of incremental changes cannot be applied."""


class VersionConflict(ChangeError):
    """Raised if the same version is used for different values of the same row."""


class ForeignKeyViolation(ChangeError, sqlite3.IntegrityError):
    """Raised if the state resulting from a batch violates a declared foreign key."""


_MISSING = object()


def _jsonable(value):
    """Turn a value into something which can be serialized as JSON deterministically."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return repr(value)


@dataclasses.dataclass
class Change:
    """
    A single incremental change for one row of a CSVW table.

    :ivar table: Name of the CSVW table (i.e. the table's `url`).
    :ivar key: Primary key of the affected row. For single-column primary keys a scalar may be \
    passed, for composite keys a sequence of values in the order of `primaryKey` is required.
    :ivar version: Increasing version number of this change.
    :ivar values: Complete new values for the row, as `dict` mapping CSVW column names to values. \
    List-valued columns take lists. `None` (or `delete=True`) signals deletion of the row.
    :ivar delete: Flag signaling deletion of the row.
    """
    table: str
    key: Any
    version: int
    values: Optional[Mapping[str, Any]] = None
    delete: bool = False

    def __post_init__(self):
        if isinstance(self.key, (list, tuple)):
            self.key = tuple(self.key)
        else:
            self.key = (self.key,)
        if self.values is None:
            # Passing no values is interpreted as deletion, too.
            self.delete = True
        if self.delete and self.values is not None:
            raise ChangeError('a change must not specify values and deletion at the same time')
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise ChangeError('version must be an integer')
        if not self.delete and not isinstance(self.values, Mapping):
            raise ChangeError('values must be a mapping of column names to values')

    @classmethod
    def fromvalue(cls, value: Union['Change', Mapping[str, Any]]) -> 'Change':
        """Create a `Change` from a `dict` or an object with corresponding attributes."""
        if isinstance(value, cls):
            return value

        def get(*names):
            for name in names:
                if isinstance(value, Mapping) and name in value:
                    return value[name]
                if not isinstance(value, Mapping) and hasattr(value, name):
                    return getattr(value, name)
            return _MISSING

        if not isinstance(value, Mapping) and not hasattr(value, 'version'):
            raise ChangeError('invalid change specification')
        table = get('table', 'table_name', 'tableName', 'resource')
        key = get('key', 'pk', 'primary_key', 'primaryKey')
        version = get('version')
        values = get('values', 'row', 'data', 'new_values')
        delete = get('delete', 'deleted', 'is_delete')
        op = get('op', 'operation', 'action')
        missing = [name for name, v in [('table', table), ('key', key), ('version', version)]
                   if v is _MISSING or v is None]
        if missing:
            raise ChangeError('missing properties in change: {}'.format(', '.join(missing)))
        if values is _MISSING:
            values = None
        if delete is _MISSING:
            delete = False
        if op is not _MISSING and op is not None:
            if str(op).lower() in {'delete', 'deleted', 'remove'}:
                delete = True
            elif str(op).lower() not in {'insert', 'update', 'upsert', 'change', 'correction'}:
                raise ChangeError(f'invalid operation in change: {op}')
        if values is None and not delete:
            raise ChangeError('a change must specify values, or be marked as deletion')
        if values is not None and delete:
            raise ChangeError('a change must not specify values and deletion at the same time')
        return cls(
            table=table,
            key=key,
            version=version,
            values=None if delete else values,
            delete=bool(delete),
        )


@dataclasses.dataclass(frozen=True)
class ApplyResult:
    """Summary of the outcome of applying a batch of changes."""
    applied: int = 0
    skipped: int = 0
    deleted: int = 0


@dataclasses.dataclass
class _PreparedChange:
    """A change, validated against the database schema and converted for storage."""
    table: str
    key: tuple
    key_json: str
    version: int
    payload: str
    values: Optional[tuple] = None
    associations: Optional[dict] = None

    @property
    def delete(self) -> bool:
        return self.values is None


class Database:
    """
    Represents a SQLite database associated with a :class:`csvw.TableGroup` instance.

    :param tg: `TableGroup` instance defining the schema of the database.
    :param fname: Path to which to write the database file.
    :param translate: Schema object name translator.
    :param drop_self_referential_fks: Flag signaling whether to drop or enforce self-referential \
    foreign-key constraints.

    .. warning::

        We write rows of a table to the database sequentially. Since CSVW does not require ordering
        rows in tables such that self-referential foreign-key constraints are satisfied at each row,
        we don't enforce self-referential foreign-keys by default in order to not trigger "false"
        integrity errors. If data in a CSVW Table is known to be ordered appropriately, `False`
        should be passed as `drop_self_referential_fks` keyword parameter to enforce
        self-referential foreign-keys.
    """
    def __init__(
            self,
            tg: TableGroup,
            fname: Optional[Union[pathlib.Path, str]] = None,
            translate: Optional[SchemaTranslator] = None,
            drop_self_referential_fks: Optional[bool] = True,
    ):
        self.translate = translate or Database.name_translator
        self.fname = pathlib.Path(fname) if fname else None
        self.init_schema(tg, drop_self_referential_fks=drop_self_referential_fks)
        self._connection = None  # For in-memory dbs we need to keep the connection!

    def init_schema(self, tg: TableGroup, drop_self_referential_fks: bool = True):
        """Inititialize the db schema, possibly ignoring self-referential foreign keys."""
        self.tg = tg
        self.tables = schema(
            self.tg, drop_self_referential_fks=drop_self_referential_fks) if self.tg else []
        self._drop_self_referential_fks = drop_self_referential_fks

    @property
    def tdict(self) -> dict[str, TableSpec]:  # pylint: disable=C0116
        return {t.name: t for t in self.tables}

    @staticmethod
    def name_translator(table: str, column: Optional[str] = None) -> str:
        """
        A callable with this signature can be passed into DB creation to control the names
        of the schema objects.

        :param table: CSVW name of the table before translation
        :param column: CSVW name of a column of `table` before translation
        :return: Translated table name if `column is None` else translated column name
        """
        # By default, no translation is done:
        return column or table

    def connection(self) -> Union[sqlite3.Connection, contextlib.closing]:
        """DB connection to be used as context manager."""
        if self.fname:
            return contextlib.closing(sqlite3.connect(str(self.fname)))
        if not self._connection:
            self._connection = sqlite3.connect(':memory:')
        return self._connection

    def _qt(self, tname: str, cname: Optional[str] = None) -> str:
        """Translate and then quote a db schema object."""
        if cname:
            return quoted(self.translate(tname, cname))
        return quoted(self.translate(tname))

    def select_many_to_many(self, db, table, context) -> dict[str, Union[tuple[str, str], str]]:
        """Select data from an association table, grouped by first foreign key."""
        qt = functools.partial(self._qt, table.name)
        if context is not None:
            context_sql = f"WHERE {qt('context')} = '{context}'"
        else:
            context_sql = ''
        sql = (f"SELECT {qt(table.columns[0].name)}, "
               f"       {qt(table.columns[1].name)}, COALESCE({qt('context')}, '') "
               f"FROM {qt()} {context_sql}")
        references = collections.defaultdict(list)
        # Keep association rows intact: keys may contain whitespace, and
        # custom contexts may contain the delimiters used by string aggregation.
        for key, value, ctx in db.execute(sql):
            references[key].append((value, ctx) if context is None else value)
        return dict(references)

    def separator(self, tname: str, cname: str) -> Optional[str]:
        """
        :return: separator for the column specified by db schema names `tname` and `cname`.
        """
        for name in self.tdict:
            if self.translate(name) == tname:
                for col in self.tdict[name].columns:
                    if self.translate(name, col.name) == cname:
                        return col.separator
        return None  # pragma: no cover

    def split_value(self, tname: str, cname: str, value) -> Union[list[str], str, None]:
        """Split a value if a separator is defined for the column."""
        sep = self.separator(tname, cname)
        return (value or '').split(sep) if sep else value

    def read(self) -> dict[str, list[collections.OrderedDict]]:
        """
        :return: A `dict` where keys are SQL table names corresponding to CSVW tables and values \
        are lists of rows, represented as dicts where keys are the SQL column names.
        """
        res = collections.defaultdict(list)
        with self.connection() as conn:
            for tname in self.tg.tabledict:
                #
                # How much do we want to use DB types? Probably as much as possible!
                # Thus we'd need to convert on write **and** read!
                #
                spec = TableReadSpec(self.tdict[tname], tname, self.translate)
                # Retrieve the many-to-many relations:
                for col, at in spec.table.many_to_many.items():
                    for pk, v in self.select_many_to_many(conn, at, col).items():
                        spec.references[pk][self.translate(tname, col)] = v

                cols, rows = select(conn, self.translate(tname))
                for row in rows:
                    res[self.translate(tname)].append(spec.read_row(zip(cols, row)))
        return res

    def association_table_context(self, _, column, fkey):
        """
        Context for association tables is created calling this method.

        Note: If a custom value for the `context` column is created by overwriting this method,
        `select_many_to_many` must be adapted accordingly, to make sure the custom
        context is retrieved when reading the data from the db.

        :param table:
        :param column:
        :param fkey:
        :return: a pair (foreign key, context)
        """
        # The default implementation takes the column name as context:
        return fkey, column

    def write_from_tg(self, _force=False, _exists_ok=False, _skip_extra=False):
        """Write the data from the contained tablegroup to a db."""
        return self.write(
            force=_force,
            _exists_ok=_exists_ok,
            _skip_extra=_skip_extra,
            **self.tg.read())

    def _get_rows(self, t, items, refs, _skip_extra):
        rows, keys = [], []
        cols = {c.name: c for c in t.columns}
        for i, row in enumerate(items):
            pk = row[t.primary_key[0]] if t.primary_key and len(t.primary_key) == 1 else None
            values = {}
            for k, v in row.items():
                if k in t.many_to_many:
                    assert pk
                    atkey = tuple([t.many_to_many[k].name] +  # noqa: W504
                                  [c.name for c in t.many_to_many[k].columns])
                    # We distinguish None - meaning NULL - and [] - meaning no items - as
                    # values of list-valued columns.
                    refs[atkey].extend([
                        tuple([pk] + list(self.association_table_context(t, k, vv)))
                        for vv in (v or [])])
                else:
                    if k not in cols:
                        if _skip_extra:
                            continue
                        raise ValueError(f'unspecified column {k} found in data')
                    col = cols[k]
                    if isinstance(v, list):
                        # Note: This assumes list-valued columns are of datatype string!
                        if col.csvw_type == 'string':
                            v = (col.separator or ';').join(
                                col.db_type.convert(vv) or '' for vv in v)
                        else:
                            v = json.dumps(v)
                    else:
                        v = col.db_type.convert(v) if v is not None else None
                    values[col.name] = v
            if i == 0:
                keys = list(values)
            elif values.keys() != set(keys):
                raise ValueError('rows must have the same columns')
            rows.append(tuple(values[k] for k in keys))
        return rows, keys

    def write(self, *, force=False, _exists_ok=False, _skip_extra=False, **items):
        """
        Creates a db file with the core schema.

        :param force: If `True` an existing db file will be overwritten.
        """
        if self.fname and self.fname.exists():
            if not force:
                raise ValueError('db file already exists, use force=True to overwrite')
            self.fname.unlink()

        with self.connection() as db:
            for table in self.tables:
                db.execute(table.sql(translate=self.translate))

            db.execute('PRAGMA foreign_keys = ON;')
            db.commit()

            refs = collections.defaultdict(list)  # collects rows in association tables.
            for t in self.tables:
                if t.name not in items:
                    continue
                rows, keys = self._get_rows(t, items[t.name], refs, _skip_extra)
                insert(db, self.translate, t.name, keys, *rows)

            for atkey, rows in refs.items():
                insert(db, self.translate, atkey[0], atkey[1:], *rows)

            db.commit()

    #
    # Incremental changes
    #
    @staticmethod
    def _versions_ddl() -> str:
        return (
            f"CREATE TABLE IF NOT EXISTS `{VERSIONS_TABLE}` ("
            f"`table_name` TEXT NOT NULL, "
            f"`key` TEXT NOT NULL, "
            f"`version` INTEGER NOT NULL, "
            f"`payload` TEXT NOT NULL, "
            f"PRIMARY KEY(`table_name`, `key`))")

    def ensure_schema(self, db: sqlite3.Connection):
        """Create the schema for the tables of the associated `TableGroup` if it does not exist."""
        for table in self.tables:
            db.execute(table.sql(translate=self.translate))
        db.execute(self._versions_ddl())

    def _table_spec(self, name: str) -> TableSpec:
        for table in self.tables:
            if table.name == name or self.translate(table.name) == name:
                return table
        raise ChangeError(f'unknown table: {name}')

    def _primary_key(self, spec: TableSpec) -> list[str]:
        if not spec.primary_key:
            raise ChangeError(f'table {spec.name} does not declare a primary key')
        return list(spec.primary_key)

    def _colspec(self, spec: TableSpec, name: str) -> ColSpec:
        """Look up a column by CSVW name, falling back to its translated DB name."""
        for col in spec.columns:
            if col.name == name or self.translate(spec.name, col.name) == name:
                return col
        raise ChangeError(f'unknown column {name} in table {spec.name}')

    @staticmethod
    def _key_json(key: tuple) -> str:
        return json.dumps([_jsonable(v) for v in key], ensure_ascii=False)

    @staticmethod
    def _convert_scalar(col: ColSpec, value):
        if value is None:
            return None
        if col.csvw_type == 'integer' and isinstance(value, str):
            try:
                value = int(value)
            except ValueError:
                pass
        elif col.csvw_type == 'string' and not isinstance(value, str):
            # A TEXT column stores non-string values as strings, so we normalize keys accordingly
            # to keep version tracking independent of how a key was passed in.
            value = str(value)
        return col.db_type.convert(value)

    def _encode_row(self, spec: TableSpec, values: Mapping[str, Any]):
        """Convert the values of a row for storage, returning scalar values and associations."""
        m2m = {}
        for colname in spec.many_to_many:
            m2m[colname] = colname
            m2m[self.translate(spec.name, colname)] = colname
        scalar, associations = {}, {}
        for key, value in values.items():
            if key in m2m:
                associations[m2m[key]] = list(value or [])
                continue
            col = self._colspec(spec, key)
            if isinstance(value, list):
                if col.csvw_type == 'string':
                    value = (col.separator or ';').join(
                        col.db_type.convert(vv) or '' for vv in value)
                else:
                    value = json.dumps([_jsonable(vv) for vv in value])
            elif col.csvw_type == 'string' and value is not None and not isinstance(value, str):
                value = str(value)
            else:
                value = self._convert_scalar(col, value)
            scalar[col.name] = value
        return scalar, associations

    def _prepare(self, change: Change) -> _PreparedChange:
        """Validate a change against the schema and convert it for storage."""
        spec = self._table_spec(change.table)
        pk = self._primary_key(spec)
        if len(change.key) != len(pk):
            raise ChangeError(
                f'table {spec.name} requires a {len(pk)}-column primary key, '
                f'got {len(change.key)} value(s)')
        key = tuple(
            self._convert_scalar(self._colspec(spec, name), value)
            for name, value in zip(pk, change.key))
        scalar = associations = None
        if change.delete:
            values = None
            payload = json.dumps(None)
        else:
            scalar, associations = self._encode_row(spec, change.values)
            missing = [c for c in pk if c not in scalar]
            if missing:
                raise ChangeError(
                    f'values for table {spec.name} must include primary key column(s) '
                    f'{", ".join(missing)}')
            for name, value in zip(pk, key):
                if scalar[name] != value:
                    raise ChangeError(
                        f'primary key value for {name} in values does not match key')
            # Columns not present in `values` are set to NULL, list-valued columns omitted are
            # reset to "no values" - a change carries the complete new state of the row.
            values = tuple(scalar.get(c.name) for c in spec.columns)
            # The payload captures the effective state of the row, so that re-applying the same
            # version is recognized as duplicate even if columns were specified differently.
            payload = json.dumps(
                {'values': list(values),
                 'associations': {c: list(associations.get(c, [])) for c in spec.many_to_many}},
                sort_keys=True,
                default=_jsonable)
        return _PreparedChange(
            table=spec.name,
            key=key,
            key_json=self._key_json(key),
            version=change.version,
            payload=payload,
            values=values,
            associations=associations,
        )

    def _select_versions(self, db: sqlite3.Connection) -> dict[tuple[str, str], tuple]:
        if not db.execute(
                "SELECT 1 FROM `sqlite_master` WHERE `type` = 'table' AND `name` = ?",
                (VERSIONS_TABLE,)).fetchone():
            return {}
        return {
            (t, k): (v, p) for t, k, v, p in db.execute(
                f"SELECT `table_name`, `key`, `version`, `payload` FROM `{VERSIONS_TABLE}`")
        }

    def apply_changes(
            self,
            changes: Optional[Union['Change', Mapping[str, Any], Iterable[Any]]] = None,
            *more: Any,
    ) -> ApplyResult:
        """
        Apply a batch of incremental changes to the database.

        Changes are applied atomically: Either all changes in the batch take effect, or the
        database - including the recorded version progress - is left untouched.

        Rows may refer to parent rows which are only created later in the same batch; declared
        foreign keys are checked against the state at the end of the batch. Deleting a row which
        is still referenced by another row is therefore rejected, while deleting a row together
        with all rows referencing it succeeds.

        Applying a change is idempotent: Changes with a version older than the recorded version
        for the same table and key are ignored. Re-applying the same version with the same values
        is a no-op, while re-using a version for different values raises
        :class:`VersionConflict`.

        :param changes: Iterable of :class:`Change` instances (or `dict` representations thereof). \
        Alternatively, changes may be passed as individual positional arguments.
        :return: An :class:`ApplyResult` summarizing the outcome.
        :raises ChangeError: If the batch is malformed or violates the schema.
        :raises VersionConflict: If the same version is used for different values of a row.
        :raises ForeignKeyViolation: If the state at the end of the batch violates a foreign key.
        """
        if changes is None:
            raw = []
        elif isinstance(changes, (Change, Mapping)):
            raw = [changes]
        else:
            raw = list(changes)
        raw.extend(more)
        prepared = [self._prepare(Change.fromvalue(c)) for c in raw]
        if not prepared:
            return ApplyResult()

        with self.connection() as db:
            previous_isolation = db.isolation_level
            previous_fk = db.execute('PRAGMA foreign_keys').fetchone()[0]
            db.isolation_level = None  # Take control of transactions.
            try:
                if db.in_transaction:
                    db.commit()
                # Foreign keys are checked against the final state of the batch, so that rows may
                # reference parents created later in the same batch.
                db.execute('PRAGMA foreign_keys = OFF')
                db.execute('BEGIN IMMEDIATE')
                try:
                    result = self._apply_prepared(db, prepared)
                    db.execute('COMMIT')
                except BaseException:
                    if db.in_transaction:
                        db.execute('ROLLBACK')
                    raise
            finally:
                if previous_fk:
                    db.execute('PRAGMA foreign_keys = ON')
                db.isolation_level = previous_isolation
        return result

    def apply_batch(self, changes=None, *more) -> ApplyResult:
        """Alias for :meth:`apply_changes`."""
        return self.apply_changes(changes, *more)

    def _apply_prepared(
            self, db: sqlite3.Connection, prepared: list[_PreparedChange]) -> ApplyResult:
        self.ensure_schema(db)
        versions = self._select_versions(db)
        applied = skipped = deleted = 0
        for change in prepared:
            known = versions.get((change.table, change.key_json))
            if known is not None:
                known_version, known_payload = known
                if change.version < known_version:
                    skipped += 1
                    continue
                if change.version == known_version:
                    if change.payload != known_payload:
                        raise VersionConflict(
                            f'version {change.version} for {change.table} {change.key} '
                            f'already exists with different values')
                    skipped += 1
                    continue
            self._apply_one(db, change)
            db.execute(
                f"INSERT INTO `{VERSIONS_TABLE}` (`table_name`, `key`, `version`, `payload`) "
                f"VALUES (?, ?, ?, ?) ON CONFLICT(`table_name`, `key`) DO UPDATE SET "
                f"`version` = excluded.`version`, `payload` = excluded.`payload`",
                (change.table, change.key_json, change.version, change.payload))
            versions[(change.table, change.key_json)] = (change.version, change.payload)
            applied += 1
            if change.delete:
                deleted += 1
        self._check_foreign_keys(db)
        return ApplyResult(applied=applied, skipped=skipped, deleted=deleted)

    def _apply_one(self, db: sqlite3.Connection, change: _PreparedChange):
        spec = self._table_spec(change.table)
        pk = self._primary_key(spec)
        qtable = self._qt(spec.name)
        where = ' AND '.join(f'{self._qt(spec.name, c)} = ?' for c in pk)
        params = tuple(change.key)
        if change.delete:
            db.execute(f"DELETE FROM {qtable} WHERE {where}", params)
        else:
            assert change.values is not None
            cols = [c.name for c in spec.columns]
            qcols = quoted(*[self.translate(spec.name, c) for c in cols])
            qvals = ','.join('?' for _ in cols)
            # An upsert keeps the row in place, so references to it stay valid.
            db.execute(
                f"INSERT INTO {qtable} ({qcols}) VALUES ({qvals}) "
                f"ON CONFLICT({quoted(*[self.translate(spec.name, c) for c in pk])}) "
                f"DO UPDATE SET " + ', '.join(
                    f'{self._qt(spec.name, c)} = excluded.{self._qt(spec.name, c)}'
                    for c in cols),
                tuple(change.values))
        # Multi-valued relations are replaced together with the row: a change describes the
        # complete new state of a row, so associations not mentioned are removed, too.
        assoc_rows = collections.defaultdict(list)
        if not change.delete:
            for colname, values in (change.associations or {}).items():
                at = spec.many_to_many[colname]
                for value in values:
                    fkey, ctx = self.association_table_context(spec.name, colname, value)
                    assoc_rows[at.name].append(tuple([change.key[0]] + list((fkey, ctx))))
        for atname in {at.name for at in spec.many_to_many.values()}:
            at = self._table_spec(atname)
            db.execute(
                f"DELETE FROM {self._qt(at.name)} WHERE "
                f"{self._qt(at.name, at.columns[0].name)} = ?",
                (change.key[0],))
            rows = assoc_rows.get(at.name)
            if rows:
                qcols = quoted(*[self.translate(at.name, c.name) for c in at.columns])
                qvals = ','.join('?' for _ in at.columns)
                db.executemany(f"INSERT INTO {self._qt(at.name)} ({qcols}) VALUES ({qvals})", rows)

    def _check_foreign_keys(self, db: sqlite3.Connection):
        # We validate all declared foreign keys explicitly - including self-referential ones,
        # which may have been dropped from the SQL schema (see `drop_self_referential_fks`).
        self._check_declared_foreign_keys(db)
        try:
            violations = db.execute('PRAGMA foreign_key_check').fetchall()
        except sqlite3.OperationalError:
            # `foreign_key_check` raises if referenced columns are not unique - a schema problem,
            # which is not caused by the batch and thus not reported here.
            return
        if violations:
            raise ForeignKeyViolation(
                'foreign key violation(s): ' + ', '.join(
                    f'{table} row {rowid} -> {parent}'
                    for table, rowid, parent, _ in violations[:5]))

    def _check_declared_foreign_keys(self, db: sqlite3.Connection):
        if not self.tg:
            return
        for table in self.tg.tables:
            spec = self.tdict.get(table.local_name)
            if spec is None or not table.tableSchema:
                continue
            colnames = {c.name for c in spec.columns}
            for fk in table.tableSchema.foreignKeys:
                if not fk.reference or fk.reference.schemaReference or not fk.reference.resource:
                    continue
                target = self.tdict.get(fk.reference.resource.string)
                src_cols = list(fk.columnReference or [])
                tgt_cols = list(fk.reference.columnReference or [])
                if target is None or not src_cols or len(src_cols) != len(tgt_cols):
                    continue
                if any(c in spec.many_to_many for c in src_cols):
                    # Association tables are covered by `PRAGMA foreign_key_check`.
                    continue
                if any(c not in colnames for c in src_cols):
                    continue
                if any(c not in {tc.name for tc in target.columns} for c in tgt_cols):
                    continue
                src_ref = [f's.{self._qt(spec.name, c)}' for c in src_cols]
                tgt_ref = [f't.{self._qt(target.name, c)}' for c in tgt_cols]
                # Null values are allowed for foreign keys, like in SQLite.
                non_null = ' AND '.join(f'{c} IS NOT NULL' for c in src_ref)
                match = ' AND '.join(f'{t} = {s}' for t, s in zip(tgt_ref, src_ref))
                sql = (
                    f"SELECT 1 FROM {self._qt(spec.name)} s WHERE {non_null} "
                    f"AND NOT EXISTS (SELECT 1 FROM {self._qt(target.name)} t WHERE {match}) "
                    f"LIMIT 1")
                if db.execute(sql).fetchone():
                    raise ForeignKeyViolation(
                        f'foreign key violation: {spec.name}.{",".join(src_cols)} references '
                        f'missing row in {target.name}')

    def read_versions(self) -> dict[tuple[str, tuple], int]:
        """Get the recorded version for each changed row, keyed by (table name, primary key)."""
        with self.connection() as db:
            versions = self._select_versions(db)
        return {
            (table, tuple(json.loads(key))): version
            for (table, key), (version, _) in versions.items()
        }


@dataclasses.dataclass
class TableReadSpec:
    """Bundles data informing the reading of table rows."""
    table: TableSpec
    name: str
    translate: SchemaTranslator
    converters: dict[str, tuple[str, Callable]] = dataclasses.field(default_factory=dict)
    separators: dict[str, str] = dataclasses.field(default_factory=dict)
    references: dict = dataclasses.field(default_factory=lambda: collections.defaultdict(dict))

    def __post_init__(self):
        # Assemble the conversion dictionary:
        for col in self.table.columns:
            if col.csvw_type in TYPE_MAP:
                conv = TYPE_MAP[col.csvw_type].read
            else:
                conv = DATATYPES[col.csvw_type].to_python
            self.converters[self.translate(self.name, col.name)] = (col.name, conv)
            if col.separator:
                if col.csvw_type == 'string':
                    self.separators[self.translate(self.name, col.name)] = col.separator
                else:
                    self.separators[self.translate(self.name, col.name)] = 'json'

    def read_row(self, row: Iterator[tuple[str, Any]]) -> collections.OrderedDict[str, Any]:
        """Read a table according to spec."""
        d = collections.OrderedDict()
        for k, v in row:
            if k in self.separators:
                if v is None:
                    d[k] = None
                elif not v:
                    d[k] = []
                elif self.separators[k] == 'json':
                    d[k] = json.loads(v)
                else:
                    d[k] = [self.converters[k][1](v_) for v_ in (v or '').split(self.separators[k])]
            else:
                d[k] = self.converters[k][1](v) if v is not None else None
        pk = d[self.translate(self.name, self.table.primary_key[0])] \
            if self.table.primary_key and len(self.table.primary_key) == 1 else None
        d.update({self.translate(self.name, k): [] for k in self.table.many_to_many})
        d.update(self.references.get(pk, {}))
        return d
