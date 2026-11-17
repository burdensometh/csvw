"""Explicit column-rename migrations for CSVW datasets.

Unifying column names across a dataset cannot be done by editing the JSON
metadata alone: column names are also referenced from CSV header rows, primary
and foreign keys, ``rowTitles`` and URI templates. This module provides
:func:`rename_columns`, which renames columns **by identity** - all renames of
one batch are resolved against the *original* schema and applied
simultaneously, so even swapping two columns' names in a single batch works -
and rewrites the affected CSV files and the metadata accordingly:

- In CSV data files, only the header cells of the renamed columns are
  replaced. All other content - in particular all data rows - is preserved
  byte-for-byte, as is the CSV dialect.
- In the metadata, column names are updated together with all references to
  them: ``primaryKey``, ``rowTitles``, both ends of ``foreignKeys`` and column
  variables in ``aboutUrl``, ``propertyUrl`` and ``valueUrl`` URI templates.

Column order, datatypes, titles, dialects and all row values are preserved, and
no string search-and-replace is performed anywhere. Name conflicts or
references that would be left dangling are reported *before* anything is
changed, so a migrated dataset can be read, validated and loaded into a
database as usual.

.. code-block:: python

    >>> from csvw import TableGroup
    >>> from csvw.migrate import rename_columns
    >>> tg = TableGroup.from_file('metadata.json')
    >>> report = rename_columns(tg, {'children.csv': {'PARENT': 'parent_id'}})
"""
import csv
import re
import codecs
import shutil
import pathlib
import tempfile
import functools
import dataclasses
import collections
from typing import Optional, Union
from collections.abc import Callable

import uritemplate

from .metadata import TableGroup, Table, Schema, URITemplate
from .dsv import UnicodeWriter
from .utils import is_url

__all__ = ['rename_columns', 'RenameError', 'RenameReport', 'ColumnRename']

#: Description attributes holding URI templates, which may reference column names.
URI_TEMPLATE_ATTRS = ['aboutUrl', 'propertyUrl', 'valueUrl']


class RenameError(ValueError):
    """Raised when a column rename migration cannot be applied."""


@dataclasses.dataclass
class ColumnRename:
    """The record of a single renamed column."""
    table: str
    old: str
    new: str


@dataclasses.dataclass
class RenameReport:
    """Describes the changes a rename migration performed (or would perform)."""
    #: The renamed columns.
    renames: list[ColumnRename]
    #: Human-readable descriptions of updated references to renamed columns.
    references: list[str]
    #: CSV files in which a header row was (or would be) rewritten.
    csv_files: list[pathlib.Path]
    #: The metadata file which was (or would be) written.
    metadata_file: Optional[pathlib.Path]
    #: Whether this report describes a dry run (no changes applied).
    dry_run: bool = False


@dataclasses.dataclass
class _Update:
    """A planned metadata update."""
    kind: str  # 'column' or 'reference'
    description: str
    apply: Callable[[], None]


@dataclasses.dataclass
class _CsvRewrite:
    """A planned rewrite of the header row of a CSV data file."""
    path: pathlib.Path
    encoding: str
    dialect: object
    lines: list[str]
    start: int  # First physical line of the header row.
    end: int  # First physical line after the header row.
    cells: list[str]  # The new header row cells.

    def execute(self) -> None:
        """Splice the new header row into the file, keeping all other lines intact."""
        with UnicodeWriter(
                None, dialect=self.dialect,
                lineterminator=_line_terminator(self.lines[self.end - 1])) as writer:
            writer.writerow(self.cells)
            header = writer.read().decode('utf-8')
        lines = self.lines[:self.start] + [header] + self.lines[self.end:]
        _replace_file(self.path, ''.join(lines).encode(self.encoding))


def _replace_file(path: pathlib.Path, content: bytes) -> None:
    """Replace a file's content, via a temporary file in the same directory."""
    with tempfile.NamedTemporaryFile(dir=str(path.parent), delete=False) as fp:
        tmp = pathlib.Path(fp.name)
    try:
        tmp.write_bytes(content)
        shutil.move(str(tmp), str(path))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _line_terminator(line: str) -> str:
    for terminator in ['\r\n', '\n', '\r']:
        if line.endswith(terminator):
            return terminator
    return ''  # The last line of a file may have no terminator.


def _read_text(path: pathlib.Path, dialect) -> tuple[str, str]:
    """Read a DSV file, returning its text and the encoding to write it back with."""
    raw = path.read_bytes()
    encoding = dialect.python_encoding
    utf8 = encoding.replace('_', '-').lower() in ['utf-8', 'utf-8-sig']
    if utf8 and raw.startswith(codecs.BOM_UTF8):
        # Keep the BOM exactly as it was.
        return raw.decode('utf-8-sig'), 'utf-8-sig'
    if encoding.replace('_', '-').lower() == 'utf-8-sig':
        # No BOM in the data, so read and write plain utf-8 to keep it that way.
        encoding = 'utf-8'
    return raw.decode(encoding), encoding


class _CountingLines:
    """An iterator over lines, counting the lines consumed so far."""

    def __init__(self, lines):
        self._lines = iter(lines)
        self.count = 0

    def __iter__(self):
        return self

    def __next__(self):
        line = next(self._lines)
        self.count += 1
        return line


def _header_row(lines: list[str], dialect) -> Optional[tuple[int, int, list[str]]]:
    """
    Locate the header row in a DSV file.

    :return: A triple ``(start, end, cells)``: the physical lines ``lines[start:end]``
    make up the header row, which parses to ``cells``; or `None` if there is no
    such row. The logic determining which rows are skipped mirrors
    :class:`csvw.dsv.UnicodeReader`.
    """
    counting = _CountingLines(lines)
    reader = csv.reader(counting, **dialect.as_python_formatting_parameters())
    start = 0
    for row in reader:
        end = counting.count
        lineno = end - 1  # 0-based line number of the row's last line, cf. UnicodeReader.
        if (row and dialect.commentPrefix and row[0].startswith(dialect.commentPrefix)) or \
                ((not row or set(row) == {''}) and dialect.skipBlankRows) or \
                (lineno < dialect.skipRows):
            start = end
            continue
        return start, end, row
    return None


def _template_variable_names(uri: str) -> set[str]:
    """The set of variable names used in a URI template."""
    names = set()
    for var in uritemplate.URITemplate(uri).variables:
        names.update(var.variable_names)
    return names


#: A URI template expression, cf. https://www.rfc-editor.org/rfc/rfc6570#section-2.2
_TEMPLATE_EXPR = re.compile(r'\{[^{}]*\}')
#: An RFC 6570 *varname*: `varchar *(["."] varchar)`:
_VARNAME = re.compile(r'([A-Za-z0-9_]|%[0-9A-Fa-f]{2})(\.?([A-Za-z0-9_]|%[0-9A-Fa-f]{2}))*')


def _rename_template_variables(uri: str, mapping: dict[str, str]) -> tuple[str, list[str]]:
    """Rename column variables in a URI template.

    Only variables which exactly match a renamed column's old name are affected;
    all literal text - including occurrences of the old name outside template
    expressions - is left untouched. All expressions are rewritten in a single
    pass, so chained renames (e.g. ``A -> B`` combined with ``B -> C``) work.

    :return: A pair ``(new_uri, invalid)``: the rewritten template and the list
    of new column names which cannot be used as URI template variable names.
    """
    invalid = []

    def rename_expr(match):
        variables = uritemplate.URITemplate(match.group(0)).variables
        if len(variables) != 1:
            return match.group(0)  # Not a proper expression - leave untouched.
        var = variables[0]
        if not any(name and name in mapping for name in var.variable_names):
            return match.group(0)
        # The operator is an enum in uritemplate>=4, a plain string before:
        operator = getattr(var.operator, 'value', var.operator)
        defaults = getattr(var, 'defaults', {})
        varspecs = []
        for name, opts in var.variables:
            spec = mapping.get(name, name)
            if name in mapping and not _VARNAME.fullmatch(spec):
                invalid.append(spec)
            if opts['explode']:
                spec += '*'
            if opts['prefix'] is not None:
                spec += f':{opts["prefix"]}'
            if name in defaults:
                spec += f'={defaults[name]}'
            varspecs.append(spec)
        return '{' + operator + ','.join(varspecs) + '}'

    return _TEMPLATE_EXPR.sub(rename_expr, uri), invalid


def _schema(table: Table) -> Optional[Schema]:
    return table.tableSchema if isinstance(table.tableSchema, Schema) else None


def _columns(table: Table) -> list:
    schema = _schema(table)
    return schema.columns if schema else []


def _map_names(mapping: dict[str, str], names: list) -> list:
    return [mapping.get(n, n) if isinstance(n, str) else n for n in names]


def _table_mappings(obj, renames, errors: list) -> tuple[dict[str, dict[str, str]], list]:
    """Resolve the user-supplied rename spec to per-table old -> new name mappings."""
    tables = obj.tables if isinstance(obj, TableGroup) else [obj]
    by_name = {t.local_name: t for t in tables}
    if not isinstance(renames, dict):
        errors.append(f'Renames must be specified as dict, not {type(renames).__name__}')
        return {}, tables
    if isinstance(obj, Table) and all(isinstance(v, str) for v in renames.values()):
        # For a single table, a flat {old: new} mapping is accepted, too.
        candidates = {obj.local_name: renames}
    else:
        candidates = renames
    mappings = {}
    for tname, mapping in candidates.items():
        if tname not in by_name:
            errors.append(
                f'Renames specified for unknown table {tname!r}; '
                f'available tables: {", ".join(sorted(by_name))}')
            continue
        if not isinstance(mapping, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in mapping.items()):
            errors.append(
                f'Invalid renames for table {tname!r}: '
                'expected a dict mapping old to new column names')
            continue
        # No-op renames (old == new) are dropped.
        mappings[tname] = {old: new for old, new in mapping.items() if old != new}
    return mappings, tables


def _validate_names(tables: list, mappings: dict, errors: list) -> None:
    """Make sure rename sources exist and rename targets are valid and unconflicted."""
    for t in tables:
        mapping = mappings.get(t.local_name)
        if not mapping:
            continue
        columns = _columns(t)
        named = {c.name for c in columns if c.name}
        for old in mapping:
            if old not in named:
                errors.append(f'{t.local_name}: no column named {old!r}')
        for new in mapping.values():
            if not new or new.startswith('_') or any(c.isspace() for c in new):
                errors.append(f'{t.local_name}: invalid new column name {new!r}')
        # Since all renames of a batch are applied simultaneously (by column identity),
        # swapping or chaining names in one batch is unproblematic - only the *final*
        # set of names must be free of duplicates:
        final = [mapping.get(c.name, c.name) for c in columns if c.name]
        for dupe, count in collections.Counter(final).items():
            if count > 1:
                errors.append(
                    f'{t.local_name}: rename would result in {count} columns named {dupe!r}')
        # New names must also not collide with the header of unnamed columns:
        nameless_headers = {c.header for c in columns if not c.name}
        for new in mapping.values():
            if new in nameless_headers:
                errors.append(
                    f'{t.local_name}: new column name {new!r} collides with the header '
                    'of an unnamed column')


def _resolves(columns: list, mapping: dict[str, str], name: str) -> bool:
    """Would `name` resolve to a column after applying `mapping`?"""
    for c in columns:
        header = mapping.get(c.name, c.name) if c.name else c.header
        if header == name:
            return True
        if c.titles and c.titles.getfirst() == name:
            return True
        if isinstance(c.propertyUrl, URITemplate) and c.propertyUrl.uri == name:
            return True
    return False


def _validate_references(tables: list, mappings: dict, errors: list) -> None:
    """Make sure no column reference is left dangling by the renames."""
    by_name = {t.local_name: t for t in tables}
    for t in tables:
        schema = _schema(t)
        if not schema:
            continue
        mapping = mappings.get(t.local_name) or {}
        for n in schema.primaryKey or []:
            if isinstance(n, str) and not _resolves(schema.columns, mapping, mapping.get(n, n)):
                errors.append(
                    f'{t.local_name}: primaryKey reference {n!r} would not resolve to a column')
        for n in schema.rowTitles:
            if isinstance(n, str) and not _resolves(schema.columns, mapping, mapping.get(n, n)):
                errors.append(
                    f'{t.local_name}: rowTitles reference {n!r} would not resolve to a column')
        for i, fk in enumerate(schema.foreignKeys):
            for n in fk.columnReference or []:
                if isinstance(n, str) and \
                        not _resolves(schema.columns, mapping, mapping.get(n, n)):
                    errors.append(
                        f'{t.local_name}: foreignKeys[{i}].columnReference {n!r} would not '
                        'resolve to a column')
            ref = fk.reference
            if ref and ref.resource and not ref.schemaReference:
                target = by_name.get(ref.resource.string)
                target_schema = _schema(target) if target else None
                if target_schema:
                    tmapping = mappings.get(target.local_name) or {}
                    for n in ref.columnReference or []:
                        if isinstance(n, str) and not _resolves(
                                target_schema.columns, tmapping, tmapping.get(n, n)):
                            errors.append(
                                f'{t.local_name}: foreignKeys[{i}].reference.columnReference '
                                f'{n!r} would not resolve to a column of table '
                                f'{target.local_name!r}')


def _validate_group_templates(obj, mappings: dict, errors: list) -> None:
    """Group-level URI templates are shared by all tables and can thus not be
    rewritten per table; renaming a column they reference must fail."""
    if not isinstance(obj, TableGroup):
        return
    renamed = {old for mapping in mappings.values() for old in mapping}
    if not renamed:
        return
    for attr in URI_TEMPLATE_ATTRS:
        tmpl = getattr(obj, attr)
        if isinstance(tmpl, URITemplate):
            try:
                hit = _template_variable_names(tmpl.uri) & renamed
            except Exception as e:  # pragma: no cover - defensive
                errors.append(f'Cannot parse {attr} of the TableGroup: {e}')
                continue
            if hit:
                errors.append(
                    f'Cannot rename column(s) {", ".join(sorted(hit))}: referenced by {attr} '
                    'of the TableGroup; move the template to the affected table(s) first')


def _plan_csv_rewrite(table: Table, mapping: dict[str, str], errors: list) -> Optional[_CsvRewrite]:
    """Plan the rewrite of a table's CSV data file, or return None if no rewrite is needed."""
    dialect = table._get_dialect()  # pylint: disable=protected-access
    if not dialect.header:
        return None  # No header row in the data file; columns are matched by position.
    non_virtual = [c for c in _columns(table) if not c.virtual]
    renamed = [(i, c) for i, c in enumerate(non_virtual) if c.name in mapping]
    if not renamed:
        return None  # Only virtual columns are renamed; they have no CSV presence.
    base = table.base
    resolved = table.url.resolve(base) if base is not None else None
    if resolved is None or is_url(str(resolved)):
        errors.append(
            f'{table.local_name}: cannot rewrite data file {table.url}: not a local file')
        return None
    path = pathlib.Path(resolved)
    if not path.exists():
        if path.parent.joinpath(path.name + '.zip').exists():
            errors.append(f'{table.local_name}: cannot rewrite zipped data file {path}')
        else:
            errors.append(f'{table.local_name}: data file {path} does not exist')
        return None
    try:
        text, encoding = _read_text(path, dialect)
    except UnicodeDecodeError as e:
        errors.append(f'{table.local_name}: cannot decode {path}: {e}')
        return None
    lines = text.splitlines(keepends=True)
    try:
        located = _header_row(lines, dialect)
    except csv.Error as e:
        errors.append(f'{table.local_name}: cannot parse {path}: {e}')
        return None
    if located is None:
        errors.append(f'{table.local_name}: no header row found in {path}')
        return None
    start, end, raw_cells = located
    # Map renamed columns to header cells - by position if the header matches the
    # schema, else by matching name or titles (cf. csvw.metadata.Schema.get_column):
    logical = [dialect.trimmer(s) for s in raw_cells][dialect.skipColumns:]
    replacements = {}  # raw cell index -> new name
    if logical == [c.header for c in non_virtual]:
        for i, col in renamed:
            replacements[i + dialect.skipColumns] = mapping[col.name]
    else:
        claimed = {}
        for i, col in renamed:
            matches = [j for j, cell in enumerate(logical) if col.has_title(cell)]
            if not matches:
                errors.append(
                    f'{table.local_name}: column {col.name!r} not found in CSV header of {path}')
                continue
            if len(matches) > 1:
                errors.append(
                    f'{table.local_name}: column {col.name!r} matches multiple cells in CSV '
                    f'header of {path}')
                continue
            if matches[0] in claimed:
                errors.append(
                    f'{table.local_name}: columns {claimed[matches[0]].name!r} and {col.name!r} '
                    f'match the same cell in CSV header of {path}')
                continue
            claimed[matches[0]] = col
            replacements[matches[0] + dialect.skipColumns] = mapping[col.name]
    # Renaming must not introduce duplicate header cells:
    final = [replacements.get(j + dialect.skipColumns, cell) for j, cell in enumerate(logical)]
    for j, new in replacements.items():
        if new in {v for k, v in enumerate(final) if k != j - dialect.skipColumns}:
            errors.append(
                f'{table.local_name}: renaming to {new!r} would duplicate a header cell '
                f'in {path}')
    if replacements:
        return _CsvRewrite(
            path=path,
            encoding=encoding,
            dialect=dialect,
            lines=lines,
            start=start,
            end=end,
            cells=[replacements.get(j, cell) for j, cell in enumerate(raw_cells)])
    return None


def _plan_updates(obj, tables: list, mappings: dict, errors: list) -> list:
    """Plan all metadata updates: column renames and synchronized references."""
    updates = []
    by_name = {t.local_name: t for t in tables}
    for t in tables:
        mapping = mappings.get(t.local_name) or {}
        schema = _schema(t)
        if not schema:
            continue
        if mapping:
            for col in schema.columns:
                if col.name in mapping:
                    updates.append(_Update(
                        kind='column',
                        description=f'{t.local_name}: rename column '
                                    f'{col.name} -> {mapping[col.name]}',
                        apply=functools.partial(setattr, col, 'name', mapping[col.name])))
            if schema.primaryKey:
                new = _map_names(mapping, schema.primaryKey)
                if new != schema.primaryKey:
                    updates.append(_Update(
                        kind='reference',
                        description=f'{t.local_name}: primaryKey {schema.primaryKey} -> {new}',
                        apply=functools.partial(setattr, schema, 'primaryKey', new)))
            if schema.rowTitles:
                new = _map_names(mapping, schema.rowTitles)
                if new != schema.rowTitles:
                    updates.append(_Update(
                        kind='reference',
                        description=f'{t.local_name}: rowTitles {schema.rowTitles} -> {new}',
                        apply=functools.partial(setattr, schema, 'rowTitles', new)))
            for i, fk in enumerate(schema.foreignKeys):
                if fk.columnReference:
                    new = _map_names(mapping, fk.columnReference)
                    if new != fk.columnReference:
                        updates.append(_Update(
                            kind='reference',
                            description=f'{t.local_name}: foreignKeys[{i}].columnReference '
                                        f'{fk.columnReference} -> {new}',
                            apply=functools.partial(setattr, fk, 'columnReference', new)))
            owners = [(t, t.local_name), (schema, f'{t.local_name}.tableSchema')]
            owners += [(c, f'{t.local_name}.columns[{c.name}]') for c in schema.columns]
            for owner, label in owners:
                for attr in URI_TEMPLATE_ATTRS:
                    tmpl = getattr(owner, attr)
                    if not isinstance(tmpl, URITemplate):
                        continue
                    try:
                        new_uri, invalid = _rename_template_variables(tmpl.uri, mapping)
                    except Exception as e:  # pragma: no cover - defensive
                        errors.append(f'{label}: cannot parse {attr} {tmpl.uri!r}: {e}')
                        continue
                    for name in invalid:
                        errors.append(
                            f'{label}: new column name {name!r} cannot be used as '
                            f'variable in the {attr} URI template {tmpl.uri!r}')
                    if new_uri != tmpl.uri and not invalid:
                        updates.append(_Update(
                            kind='reference',
                            description=f'{label}: {attr} {tmpl.uri} -> {new_uri}',
                            apply=functools.partial(
                                setattr, owner, attr, URITemplate(new_uri))))
        # The target ends of foreign keys are driven by the *referenced* table's renames:
        for i, fk in enumerate(schema.foreignKeys):
            ref = fk.reference
            if ref and ref.resource and not ref.schemaReference and ref.columnReference:
                target = by_name.get(ref.resource.string)
                tmapping = mappings.get(target.local_name) if target else None
                if tmapping:
                    new = _map_names(tmapping, ref.columnReference)
                    if new != ref.columnReference:
                        updates.append(_Update(
                            kind='reference',
                            description=f'{t.local_name}: foreignKeys[{i}].reference.'
                                        f'columnReference {ref.columnReference} -> {new} '
                                        f'(columns of {target.local_name})',
                            apply=functools.partial(
                                setattr, ref, 'columnReference', new)))
    return updates


def rename_columns(
        obj: Union[TableGroup, Table],
        renames: dict,
        dry_run: bool = False,
) -> RenameReport:
    """Rename columns of a CSVW dataset, in the metadata **and** the CSV data files.

    All renames of one batch are resolved against the original schema and applied
    simultaneously - by column identity, not by string replacement - so swapping
    two columns' names in a single batch works as expected. References to renamed
    columns in ``primaryKey``, ``rowTitles``, both ends of ``foreignKeys`` and in
    ``aboutUrl``/``propertyUrl``/``valueUrl`` URI templates are updated in sync,
    and the header rows of the affected CSV data files are rewritten in place.
    Column order, datatypes, titles, dialects and all row values are preserved.

    :param obj: A :class:`csvw.TableGroup` or :class:`csvw.Table`, typically \
    read from a metadata file via :meth:`from_file`.
    :param renames: For a `TableGroup`, a dict mapping table names (i.e. the \
    tables' `url` values) to dicts of old -> new column names; for a single \
    `Table`, a dict of old -> new column names.
    :param dry_run: If `True`, only plan the migration and report what would \
    change, without modifying anything.
    :return: A :class:`RenameReport` describing the changes.
    :raises RenameError: if the renames cannot be applied - e.g. unknown tables \
    or columns, invalid or conflicting new names, or references that would be \
    left dangling. All problems are validated and reported *before* anything \
    is changed.
    """
    if not isinstance(obj, (TableGroup, Table)):
        raise TypeError('rename_columns requires a TableGroup or Table instance')
    errors = []
    mappings, tables = _table_mappings(obj, renames, errors)
    _validate_names(tables, mappings, errors)
    _validate_references(tables, mappings, errors)
    _validate_group_templates(obj, mappings, errors)
    rewrites = []
    for t in tables:
        if mappings.get(t.local_name):
            rewrite = _plan_csv_rewrite(t, mappings[t.local_name], errors)
            if rewrite:
                rewrites.append(rewrite)
    updates = _plan_updates(obj, tables, mappings, errors)
    if errors:
        raise RenameError(
            'Cannot rename columns:\n' + '\n'.join(f'- {e}' for e in errors))
    if not dry_run:
        for rewrite in rewrites:
            rewrite.execute()
        for update in updates:
            update.apply()
    metadata_file = getattr(obj, '_fname', None)  # pylint: disable=protected-access
    if not dry_run and updates and metadata_file:
        obj.to_file(metadata_file)
    return RenameReport(
        renames=[ColumnRename(t.local_name, old, new)
                 for t in tables for old, new in (mappings.get(t.local_name) or {}).items()],
        references=[u.description for u in updates if u.kind == 'reference'],
        csv_files=[r.path for r in rewrites],
        metadata_file=pathlib.Path(metadata_file) if (metadata_file and updates) else None,
        dry_run=dry_run)
