import time
import decimal
import pathlib
import sqlite3
import warnings
from datetime import date

import pytest

from csvw import TableGroup, Column, Table, ForeignKey
from csvw.metadata import DATATYPES, Datatype
from csvw.db import (
    Database, Change, ApplyResult, ChangeError, VersionConflict, ForeignKeyViolation)

FIXTURES = pathlib.Path(__file__).parent / 'fixtures'


@pytest.fixture
def tg():
    return TableGroup.fromvalue({'tables': [{'url': 'data', 'tableSchema': {'columns': []}}]})


@pytest.fixture
def translate():
    def _t(t, c=None):
        if t == 'data' and c == 'v':
            return 'vv'
        if c:
            return c
        return t
    return _t


@pytest.mark.parametrize('datatype', [dt for dt in DATATYPES if dt != 'time'])
def test_datatypes(tg, datatype):
    datatype = DATATYPES[datatype]
    tg.tables[0].tableSchema.columns.extend([
        Column.fromvalue({'datatype': datatype.name, 'name': 'v1'}),
        Column.fromvalue({'datatype': datatype.name, 'name': 'v2'}),
    ])
    db = Database(tg)
    v = datatype.to_python(datatype.example)
    db.write(data=[{'v1': v, 'v2': None}])
    data = db.read()['data']
    assert data[0]['v1'] == v
    assert data[0]['v2'] is None
    db._connection.close()


@pytest.mark.parametrize('datatype, values', [
    ('boolean', [True, False, None]),
    ('decimal', [decimal.Decimal('1.25'), decimal.Decimal('-2.5'), None]),
])
def test_read_preserves_declared_scalar_types(tg, datatype, values):
    tg.tables[0].tableSchema.columns.append(Column.fromvalue({'datatype': datatype, 'name': 'v'}))
    db = Database(tg)
    db.write(data=[{'v': value} for value in values])
    actual = [row['v'] for row in db.read()['data']]
    assert actual == values
    assert [type(value) for value in actual] == [type(value) for value in values]
    db._connection.close()


def test_list_valued(tg, translate):
    tg.tables[0].tableSchema.columns.append(Column.fromvalue({'separator': '#', 'name': 'v'}))
    db = Database(tg, translate=translate)
    with pytest.raises(TypeError):
        db.write(data=[{'v': [1, 2, 3]}])
    db.write(data=[{'v': ['a', 'b', ' c']}, {'v': []}])
    assert db.split_value('data', 'vv', 'a#b#c') == ['a', 'b', 'c']
    data = db.read()['data']
    assert data[0]['vv'] == ['a', 'b', ' c']
    assert data[1]['vv'] == []
    db.write(data=[{'v': ['a', 'b', None]}])
    db._connection.close()


def test_list_valued_non_text(tg, translate):
    tg.tables[0].tableSchema.columns.append(Column.fromvalue(
        {'separator': '#', 'name': 'v', 'datatype': 'integer'}))
    db = Database(tg, translate=translate)
    db.write(data=[{'v': [1, 2, 3]}, {'v': None}])
    data = db.read()['data']
    assert data[0]['vv'] == [1, 2, 3]
    assert data[1]['vv'] is None
    db._connection.close()


@pytest.mark.parametrize('reverse_first', [False, True])
def test_write_row_column_order(tg, reverse_first):
    tg.tables[0].tableSchema.columns.extend([
        Column.fromvalue({'name': 'ID'}),
        Column.fromvalue({'name': 'Form'}),
    ])
    rows = [{'ID': '1', 'Form': 'house'}, {'Form': 'water', 'ID': '2'}]
    if reverse_first:
        rows.reverse()
    db = Database(tg)
    db.write(data=iter(rows))
    assert db.read()['data'] == rows
    db._connection.close()


@pytest.mark.parametrize('second', [{'ID': '2'}, {'ID': '2', 'Other': 'water'}])
def test_write_inconsistent_row_columns(tg, second):
    tg.tables[0].tableSchema.columns.extend([
        Column.fromvalue({'name': name}) for name in ['ID', 'Form', 'Other']
    ])
    db = Database(tg)
    with pytest.raises(ValueError, match='same columns'):
        db.write(data=[{'ID': '1', 'Form': 'house'}, second])
    db._connection.close()


def test_required(tg):
    tg.tables[0].tableSchema.columns.append(Column.fromvalue({'required': 'True', 'name': 'v'}))
    db = Database(tg)
    with pytest.raises(sqlite3.IntegrityError):
        db.write(data=[{'v': None}])
    db._connection.close()


@pytest.mark.parametrize(
    'datatype,value,error',
    [
        ({'base': 'decimal', 'minimum': -90, 'maximum': 90}, -100, True),
        ({'base': 'decimal', 'minimum': -90, 'maximum': 90}, 100, True),
        ({'base': 'decimal', 'minimum': -90, 'maximum': 90}, -10, False),
        ({'base': 'string', 'length': 3}, 'ab', True),
        ({'base': 'string', 'length': 3}, 'abcd', True),
        ({'base': 'string', 'length': 3}, 'abc', False),
        ({'base': 'string', 'minLength': 3}, 'ab', True),
        ({'base': 'string', 'minLength': 3}, 'abc', False),
        ({'base': 'string', 'maxLength': 3}, 'abcd', True),
        ({'base': 'string', 'maxLength': 3}, 'abc', False),
        ({'base': 'date', 'minimum': '2019-02-01'}, date(year=2018, month=2, day=1), True),
        ({'base': 'date', 'maximum': '2019-02-01'}, date(year=2018, month=2, day=1), False),
    ]
)
def test_constraints(tg, datatype, value, error):
    tg.tables[0].tableSchema.columns.append(Column.fromvalue({'datatype': datatype, 'name': 'v'}))
    db = Database(tg)
    if error:
        with pytest.raises(sqlite3.IntegrityError):
            db.write(data=[{'v': value}])
    else:
        db.write(data=[{'v': value}])
    db._connection.close()


def test_file(tmp_path, tg):
    fname = tmp_path / 'test.sqlite'
    tg.tables[0].tableSchema.columns.append(Column.fromvalue({'name': 'v'}))
    db = Database(tg, fname=fname)
    db.write()
    assert fname.exists()
    with pytest.raises(ValueError):
        db.write()


def test_extra_columns(tmp_path):
    tmp_path.joinpath('md.json').write_text("""{
    "@context": ["http://www.w3.org/ns/csvw",{"@language": "en"}],
    "dialect": {"header": true,"encoding": "utf-8-sig"},
    "tables": [
        {"url": "csv.txt","tableSchema": {"columns": [{"name": "ID", "datatype": "string"}]}}
    ]
}
""", encoding='utf8')

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")

        tmp_path.joinpath('csv.txt').write_text('ID,extra\n1,ex', encoding='utf8')
        tg = TableGroup.from_file(str(tmp_path.joinpath('md.json')))
        db = Database(tg, fname=tmp_path / 'test.sqlite')
        with pytest.raises(ValueError):
            db.write_from_tg()
        db.write_from_tg(_force=True, _skip_extra=True)


def test_foreign_keys(tg, translate):
    tg.tables[0].tableSchema.columns.append(Column.fromvalue({'name': 'v'}))
    tg.tables[0].tableSchema.primaryKey = ['v']
    tg.tables.append(Table.fromvalue({
        'url': 'ref',
        'tableSchema': {
            'columns': [{'name': 'ref'}],
            'foreignKeys': [
                {
                    'columnReference': 'ref',
                    'reference': {
                        'resource': 'data',
                        'columnReference': 'v',
                    }
                }
            ]
        }
    }))
    db = Database(tg, translate=translate)
    with pytest.raises(sqlite3.IntegrityError):
        db.write(ref=[{'ref': 'y'}], data=[{'v': 'x'}])
    db.write(ref=[{'ref': 'x'}], data=[{'v': 'x'}])
    # 'vv' is used as column name as specified by the translate fixture:
    assert 'vv' in db.read()['data'][0]
    db._connection.close()


def test_self_referential_foreign_keys(tg):
    tg.tables[0].tableSchema.columns.append(Column.fromvalue({'name': 'id'}))
    tg.tables[0].tableSchema.columns.append(Column.fromvalue({'name': 'ref'}))
    tg.tables[0].tableSchema.primaryKey = ['id']
    tg.tables[0].tableSchema.foreignKeys.append(
        ForeignKey.fromdict(dict(
            reference=dict(resource='data', columnReference=['id']),
            columnReference=['ref']
        )))
    db = Database(tg)
    db.write(data=[{'id': '1', 'ref': '2'}, {'id': '2', 'ref': None}])
    assert len(db.read()['data']) == 2
    db._connection.close()

    db = Database(tg, drop_self_referential_fks=False)
    with pytest.raises(sqlite3.IntegrityError):
        db.write(data=[{'id': '1', 'ref': '2'}, {'id': '2', 'ref': None}])
    db._connection.close()

    db = Database(tg, drop_self_referential_fks=False)
    db.write(data=[{'id': '2', 'ref': None}, {'id': '1', 'ref': '2'}])
    db._connection.close()


@pytest.mark.parametrize('columns,reference', [
    (['left', 'right'], ['zeta', 'alpha']),
    (['right', 'left'], ['alpha', 'zeta']),
])
def test_composite_foreign_key_order(columns, reference):
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'parent', 'tableSchema': {
            'columns': [{'name': 'alpha'}, {'name': 'zeta'}],
            'primaryKey': ['alpha', 'zeta'],
        }},
        {'url': 'child', 'tableSchema': {
            'columns': [{'name': 'left'}, {'name': 'right'}],
            'foreignKeys': [{
                'columnReference': columns,
                'reference': {'resource': 'parent', 'columnReference': reference},
            }],
        }},
    ]})
    db = Database(tg)
    db.write(parent=[{'alpha': 'a', 'zeta': 'z'}], child=[{'left': 'z', 'right': 'a'}])
    assert db.read()['child'] == [{'left': 'z', 'right': 'a'}]
    with db.connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO child VALUES (?, ?)', ('a', 'z'))
        conn.execute('DELETE FROM parent')
        assert conn.execute('SELECT * FROM child').fetchall() == []
    db._connection.close()


@pytest.fixture
def tg_with_foreign_keys(tg):
    tg.tables[0].tableSchema.columns.append(Column.fromvalue({'name': 'v'}))
    tg.tables[0].tableSchema.primaryKey = ['v']
    tg.tables.append(Table.fromvalue({
        'url': 'ref',
        'tableSchema': {
            # Define two list-valued foreign key:
            'columns': [
                {'name': 'pk'},
                {'name': 'ref1', 'separator': ';', 'null': ['NA']},
                {'name': 'ref2', 'separator': ';'},
            ],
            'foreignKeys': [
                {
                    'columnReference': 'ref1',
                    'reference': {
                        'resource': 'data',
                        'columnReference': 'v',
                    }
                },
                {
                    'columnReference': 'ref2',
                    'reference': {
                        'resource': 'data',
                        'columnReference': 'v',
                    }
                }
            ],
            'primaryKey': ['pk']
        }
    }))
    return tg


def test_many_to_many(tg_with_foreign_keys):
    db = Database(tg_with_foreign_keys)
    with pytest.raises(sqlite3.IntegrityError):
        # Foreign key violates referential integrity:
        db.write(ref=[{'pk': '1', 'ref1': ['y']}], data=[{'v': 'x'}])

    db.write(ref=[{'pk': '1', 'ref1': ['y', 'x']}], data=[{'v': 'x'}, {'v': 'y'}])
    res = db.read()['ref'][0]
    # Associations between the same pair of tables are grouped by foreign key column:
    assert res['ref1'] == ['y', 'x']
    assert res['ref2'] == []
    db._connection.close()


@pytest.mark.parametrize('translate_context', [False, True])
@pytest.mark.parametrize('populated', [False, True])
def test_many_to_many_translated_columns(tg_with_foreign_keys, translate_context, populated):
    def translate(table, column=None):
        if column == 'context' and not translate_context:
            return column
        return 'c_' + column if column is not None else 't_' + table

    db = Database(tg_with_foreign_keys, translate=translate)
    values = ['y', 'x'] if populated else []
    db.write(ref=[{'pk': '1', 'ref1': values, 'ref2': []}], data=[{'v': 'x'}, {'v': 'y'}])
    assert db.read()['t_ref'] == [{'c_pk': '1', 'c_ref1': values, 'c_ref2': []}]
    db._connection.close()


@pytest.mark.parametrize('keys', [
    ['New York', 'San Francisco'],
    ['two\twords', 'line\nbreak'],
    ['eau de vie', 'New York', 'eau de vie'],
])
def test_many_to_many_whitespace(tg_with_foreign_keys, keys):
    db = Database(tg_with_foreign_keys)
    db.write(ref=[{'pk': '1', 'ref1': keys, 'ref2': keys[::-1]}],
             data=[{'v': key} for key in dict.fromkeys(keys)])
    res = db.read()['ref'][0]
    assert res['ref1'] == keys
    assert res['ref2'] == keys[::-1]
    db._connection.close()


def test_many_to_many_2(tg_with_foreign_keys):
    db = Database(tg_with_foreign_keys)
    db.write(
        ref=[
            {'pk': '1', 'ref1': ['y', 'x']},
            {'pk': '2', 'ref1': ['x']},
        ],
        data=[{'v': 'x'}, {'v': 'y'}])

    res = db.read()['ref'][0]
    # Associations between the same pair of tables are grouped by foreign key column:
    assert res['ref1'] == ['y', 'x']
    assert res['ref2'] == []
    res = db.read()['ref'][1]
    # Associations between the same pair of tables are grouped by foreign key column:
    assert res['ref1'] == ['x']
    db._connection.close()


def test_many_to_many_no_context(tg_with_foreign_keys):
    class DatabaseWithoutContext(Database):
        def association_table_context(self, table, column, fkey):
            return fkey, '1234'

        def select_many_to_many(self, db, table, context):
            # Tables with at most one foreign key to another table can use the context
            # to store something else.
            return Database.select_many_to_many(self, db, table, None)

    db = DatabaseWithoutContext(tg_with_foreign_keys)
    db.write(ref=[{'pk': '1', 'ref1': ['y', 'x']}], data=[{'v': 'x'}, {'v': 'y'}])
    res = db.read()['ref'][0]
    # The context will then be returned for **each** foreign key column!
    assert res['ref2'] == [('y', '1234'), ('x', '1234')]
    db._connection.close()


def test_many_to_many_context_delimiter(tg_with_foreign_keys):
    class DatabaseWithoutContext(Database):
        def association_table_context(self, table, column, fkey):
            return fkey, 'part1||part2'

        def select_many_to_many(self, db, table, context):
            return Database.select_many_to_many(self, db, table, None)

    db = DatabaseWithoutContext(tg_with_foreign_keys)
    db.write(ref=[{'pk': '1', 'ref1': ['New York', 'San Francisco']}],
             data=[{'v': 'New York'}, {'v': 'San Francisco'}])
    assert db.read()['ref'][0]['ref1'] == [
        ('New York', 'part1||part2'), ('San Francisco', 'part1||part2')]
    db._connection.close()


def test_many_to_many_self_referential(tg):
    tg.tables[0].tableSchema.columns.append(Column.fromvalue({'name': 'v'}))
    tg.tables[0].tableSchema.columns.append(Column.fromvalue({'name': 'ref', 'separator': ';'}))
    tg.tables[0].tableSchema.primaryKey = ['v']
    tg.tables[0].tableSchema.foreignKeys.append(ForeignKey.fromdict({
        'columnReference': 'ref',
        'reference': {
            'resource': 'data',
            'columnReference': 'v',
        }
    }))
    db = Database(tg)
    with pytest.raises(sqlite3.IntegrityError):
        db.write(data=[{'v': 'x', 'ref': ['y']}])
    db.write(data=[{'v': 'x', 'ref': []}, {'v': 'y', 'ref': ['x', 'y']}])
    assert db.read()['data'][1]['ref'] == ['x', 'y']
    db._connection.close()


def test_integration():
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        tg = TableGroup.from_file(FIXTURES / 'csv.txt-metadata.json')
        orig = tg.read()
        db = Database(tg)
        db.write_from_tg()
        for table, items in db.read().items():
            assert items == orig[table]
        db._connection.close()


def test_write_file_exists(tmp_path):
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        target = tmp_path / 'db.sqlite3'
        target.touch(exist_ok=False)
        mtime = target.stat().st_mtime
        tg = TableGroup.from_file(FIXTURES / 'csv.txt-metadata.json')
        db = Database(tg, fname=target)
        with pytest.raises(ValueError, match=r'already exists'):
            db.write()
        time.sleep(0.1)
        db.write(force=True)
        assert target.stat().st_mtime > mtime


@pytest.mark.parametrize('reverse', [False, True])
def test_large_acyclic_table_group(reverse):
    tables = []
    items = {}
    for i in range(103):
        schema = {'columns': [{'name': 'ID', 'datatype': 'integer'}], 'primaryKey': 'ID'}
        row = {'ID': i}
        if i:
            schema['columns'].append({'name': 'parent', 'datatype': 'integer'})
            schema['foreignKeys'] = [{
                'columnReference': 'parent',
                'reference': {'resource': f't{i - 1}', 'columnReference': 'ID'},
            }]
            row['parent'] = i - 1
        tables.append({'url': f't{i}', 'tableSchema': schema})
        items[f't{i}'] = [row]
    if reverse:
        tables.reverse()
    db = Database(TableGroup.fromvalue({'tables': tables}))
    assert [table.name for table in db.tables] == [f't{i}' for i in range(103)]
    db.write(**items)
    assert db.read() == items
    db._connection.close()


def test_cyclic_table_group():
    tables = []
    for name, target in [('a', 'b'), ('b', 'a')]:
        tables.append({'url': name, 'tableSchema': {
            'columns': [{'name': 'ID'}], 'primaryKey': 'ID',
            'foreignKeys': [{'columnReference': 'ID', 'reference': {
                'resource': target, 'columnReference': 'ID'}}],
        }})
    with pytest.raises(ValueError, match='cyclic dependencies'):
        Database(TableGroup.fromvalue({'tables': tables}))


#
# Incremental changes
#
@pytest.fixture
def change_tg():
    """A TableGroup with a parent table, a child table and two list-valued foreign keys."""
    return TableGroup.fromvalue({'tables': [
        {'url': 'parent', 'tableSchema': {
            'columns': [{'name': 'id'}, {'name': 'label'}],
            'primaryKey': ['id'],
        }},
        {'url': 'child', 'tableSchema': {
            'columns': [
                {'name': 'id'},
                {'name': 'pid'},
                {'name': 'ref1', 'separator': ';'},
                {'name': 'ref2', 'separator': ';'},
            ],
            'primaryKey': ['id'],
            'foreignKeys': [
                {
                    'columnReference': ['pid'],
                    'reference': {'resource': 'parent', 'columnReference': ['id']},
                },
                {
                    'columnReference': ['ref1'],
                    'reference': {'resource': 'parent', 'columnReference': ['id']},
                },
                {
                    'columnReference': ['ref2'],
                    'reference': {'resource': 'parent', 'columnReference': ['id']},
                },
            ],
        }},
    ]})


def read_rows(db):
    try:
        rows = db.read()
    except sqlite3.OperationalError:
        # The db schema is only written when the first change is applied successfully.
        return {}
    res = {t: [] for t in db.tg.tabledict}
    res.update({t: [dict(r) for r in values] for t, values in rows.items()})
    return res


def test_apply_changes_insert_update_delete(change_tg):
    db = Database(change_tg)
    res = db.apply_changes([
        Change('parent', 'p1', 1, {'id': 'p1', 'label': 'one'}),
        Change('parent', 'p2', 1, {'id': 'p2', 'label': 'two'}),
    ])
    assert res == ApplyResult(applied=2, skipped=0, deleted=0)
    assert read_rows(db)['parent'] == [
        {'id': 'p1', 'label': 'one'}, {'id': 'p2', 'label': 'two'}]

    res = db.apply_changes(Change('parent', 'p1', 2, {'id': 'p1', 'label': 'ONE'}))
    assert res.applied == 1
    rows = {r['id']: r for r in read_rows(db)['parent']}
    assert rows['p1']['label'] == 'ONE'

    res = db.apply_changes(Change('parent', 'p2', 2, delete=True))
    assert res.deleted == 1
    assert [r['id'] for r in read_rows(db)['parent']] == ['p1']
    db._connection.close()


def test_apply_changes_older_and_duplicate_versions(change_tg):
    db = Database(change_tg)
    db.apply_changes([Change('parent', 'p1', 5, {'id': 'p1', 'label': 'current'})])
    # An older version does not overwrite the current state:
    assert db.apply_changes(
        [Change('parent', 'p1', 4, {'id': 'p1', 'label': 'stale'})]).skipped == 1
    # Repeating the same version with identical values is a no-op:
    assert db.apply_changes(
        [Change('parent', 'p1', 5, {'id': 'p1', 'label': 'current'})]).skipped == 1
    assert read_rows(db)['parent'] == [{'id': 'p1', 'label': 'current'}]
    # Repeating the same version with different values is an error:
    with pytest.raises(VersionConflict):
        db.apply_changes([Change('parent', 'p1', 5, {'id': 'p1', 'label': 'other'})])
    assert read_rows(db)['parent'] == [{'id': 'p1', 'label': 'current'}]
    db._connection.close()


def test_apply_changes_same_version_in_batch(change_tg):
    db = Database(change_tg)
    with pytest.raises(VersionConflict):
        db.apply_changes([
            Change('parent', 'p1', 1, {'id': 'p1', 'label': 'one'}),
            Change('parent', 'p1', 1, {'id': 'p1', 'label': 'two'}),
        ])
    assert read_rows(db).get('parent', []) == []
    db._connection.close()


def test_apply_changes_forward_reference_within_batch(change_tg):
    db = Database(change_tg)
    # The child row refers to a parent which is only created later in the same batch:
    res = db.apply_changes([
        Change('child', 'c1', 1, {'id': 'c1', 'pid': 'p1', 'ref1': ['p1', 'p2']}),
        Change('parent', 'p1', 1, {'id': 'p1', 'label': 'one'}),
        Change('parent', 'p2', 1, {'id': 'p2', 'label': 'two'}),
    ])
    assert res.applied == 3
    rows = read_rows(db)
    assert rows['child'] == [
        {'id': 'c1', 'pid': 'p1', 'ref1': ['p1', 'p2'], 'ref2': []}]
    db._connection.close()


def test_apply_changes_missing_parent_rolls_back(change_tg):
    db = Database(change_tg)
    db.apply_changes([Change('parent', 'p1', 1, {'id': 'p1', 'label': 'one'})])
    with pytest.raises(ForeignKeyViolation):
        db.apply_changes([
            Change('parent', 'p3', 1, {'id': 'p3', 'label': 'three'}),
            # 'missing' is never created:
            Change('child', 'c1', 1, {'id': 'c1', 'pid': 'missing'}),
        ])
    # Nothing from the failed batch is applied - neither data nor version progress:
    assert read_rows(db)['parent'] == [{'id': 'p1', 'label': 'one'}]
    assert db.read_versions() == {('parent', ('p1',)): 1}
    db._connection.close()


def test_apply_changes_delete_referenced_parent_rejected(change_tg):
    db = Database(change_tg)
    db.apply_changes([
        Change('parent', 'p1', 1, {'id': 'p1', 'label': 'one'}),
        Change('child', 'c1', 1, {'id': 'c1', 'pid': 'p1', 'ref1': ['p1']}),
    ])
    with pytest.raises(ForeignKeyViolation):
        db.apply_changes([Change('parent', 'p1', 2, delete=True)])
    assert read_rows(db)['parent'] == [{'id': 'p1', 'label': 'one'}]
    assert db.read_versions()[('parent', ('p1',))] == 1

    # Deleting parent and referencing child in the same batch succeeds:
    res = db.apply_changes([
        Change('child', 'c1', 2, delete=True),
        Change('parent', 'p1', 3, delete=True),
    ])
    assert res.deleted == 2
    assert read_rows(db)['parent'] == []
    assert read_rows(db)['child'] == []
    db._connection.close()


def test_apply_changes_list_valued_references_updated_with_row(change_tg):
    db = Database(change_tg)
    db.apply_changes([
        Change('parent', 'p1', 1, {'id': 'p1', 'label': 'one'}),
        Change('parent', 'p2', 1, {'id': 'p2', 'label': 'two'}),
        Change('parent', 'p3', 1, {'id': 'p3', 'label': 'three'}),
        Change('child', 'c1', 1, {'id': 'c1', 'ref1': ['p1', 'p2'], 'ref2': ['p2']}),
    ])
    row = read_rows(db)['child'][0]
    assert row['ref1'] == ['p1', 'p2']
    assert row['ref2'] == ['p2']

    # Updating the row replaces its multi-valued relations:
    db.apply_changes(
        Change('child', 'c1', 2, {'id': 'c1', 'ref1': ['p3'], 'ref2': []}))
    row = read_rows(db)['child'][0]
    assert row['ref1'] == ['p3']
    assert row['ref2'] == []

    # A list-valued reference to a missing parent is rejected:
    with pytest.raises(ForeignKeyViolation):
        db.apply_changes(
            Change('child', 'c1', 3, {'id': 'c1', 'ref1': ['nope']}))
    assert read_rows(db)['child'][0]['ref1'] == ['p3']
    assert db.read_versions()[('child', ('c1',))] == 2
    db._connection.close()


def test_apply_changes_self_referential(change_tg):
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'nodes', 'tableSchema': {
            'columns': [{'name': 'id'}, {'name': 'parent'}],
            'primaryKey': ['id'],
            'foreignKeys': [{
                'columnReference': ['parent'],
                'reference': {'resource': 'nodes', 'columnReference': ['id']},
            }],
        }},
    ]})
    db = Database(tg)
    # Within the batch, a row may refer to a parent created later:
    db.apply_changes([
        Change('nodes', 'b', 1, {'id': 'b', 'parent': 'a'}),
        Change('nodes', 'a', 1, {'id': 'a', 'parent': None}),
    ])
    assert {r['id']: r['parent'] for r in read_rows(db)['nodes']} == {'a': None, 'b': 'a'}
    # Deleting a referenced node is rejected, even if self-referential FKs are dropped from SQL:
    with pytest.raises(ForeignKeyViolation):
        db.apply_changes([Change('nodes', 'a', 2, delete=True)])
    db._connection.close()


def test_apply_changes_composite_primary_key():
    tg = TableGroup.fromvalue({'tables': [
        {'url': 't', 'tableSchema': {
            'columns': [{'name': 'a'}, {'name': 'b'}, {'name': 'v'}],
            'primaryKey': ['a', 'b'],
        }},
    ]})
    db = Database(tg)
    db.apply_changes([
        Change('t', ('a1', 'b1'), 1, {'a': 'a1', 'b': 'b1', 'v': 'x'}),
        Change('t', ['a1', 'b2'], 1, {'a': 'a1', 'b': 'b2', 'v': 'y'}),
    ])
    assert sorted((r['a'], r['b'], r['v']) for r in read_rows(db)['t']) == [
        ('a1', 'b1', 'x'), ('a1', 'b2', 'y')]
    # A key of the wrong arity is rejected:
    with pytest.raises(ChangeError):
        db.apply_changes([Change('t', 'a1', 2, {'a': 'a1', 'b': 'b1', 'v': 'z'})])
    # Keys are converted according to the declared datatype:
    with pytest.raises(ChangeError):
        db.apply_changes([Change('t', ('a1', 'b1'), 2, {'a': 'a1', 'b': 'bX', 'v': 'z'})])
    db._connection.close()


def test_apply_changes_datatypes_and_translation(change_tg):
    def translate(table, column=None):
        return ('t_' + table) if column is None else ('c_' + column)

    change_tg.tables[0].tableSchema.columns[0].datatype = Datatype.fromvalue('integer')
    db = Database(change_tg, translate=translate)
    db.apply_changes([
        Change('parent', 1, 1, {'id': 1, 'label': 'one'}),
        Change('child', 'c1', 1, {'id': 'c1', 'pid': 1, 'ref1': ['1']}),
    ])
    assert read_rows(db)['t_parent'] == [{'c_id': 1, 'c_label': 'one'}]
    # Only the `parent.id` column is typed as integer; `child.pid` remains a string:
    assert read_rows(db)['t_child'] == [
        {'c_id': 'c1', 'c_pid': '1', 'c_ref1': ['1'], 'c_ref2': []}]
    # Using translated names in change specs works as well:
    db.apply_changes(Change('t_parent', 1, 2, {'c_id': 1, 'c_label': 'eins'}))
    assert read_rows(db)['t_parent'] == [{'c_id': 1, 'c_label': 'eins'}]
    db._connection.close()


def test_apply_changes_dict_specs(change_tg):
    db = Database(change_tg)
    db.apply_changes([
        {'table': 'parent', 'key': 'p1', 'version': 1, 'values': {'id': 'p1', 'label': 'one'}},
        {'table_name': 'parent', 'pk': 'p2', 'version': 1,
         'row': {'id': 'p2', 'label': 'two'}},
    ])
    assert len(read_rows(db)['parent']) == 2
    db.apply_changes({'table': 'parent', 'key': 'p2', 'version': 2, 'delete': True})
    assert [r['id'] for r in read_rows(db)['parent']] == ['p1']
    # Malformed specs are rejected:
    for spec in [
        {'table': 'parent', 'version': 1, 'values': {'id': 'p3', 'label': 'x'}},
        {'table': 'parent', 'key': 'p3', 'values': {'id': 'p3', 'label': 'x'}},
        {'key': 'p3', 'version': 1, 'values': {'id': 'p3', 'label': 'x'}},
        {'table': 'unknown', 'key': 'p3', 'version': 1, 'values': {'id': 'p3'}},
        {'table': 'parent', 'key': 'p3', 'version': 1, 'values': {'id': 'p3', 'oops': 'x'}},
        {'table': 'parent', 'key': 'p3', 'version': 1},
    ]:
        with pytest.raises(ChangeError):
            db.apply_changes([spec])
    db._connection.close()


def test_apply_changes_initialise_schema(tmp_path, change_tg):
    fname = tmp_path / 'db.sqlite'
    db = Database(change_tg, fname=fname)
    db.apply_changes([Change('parent', 'p1', 1, {'id': 'p1', 'label': 'one'})])
    assert fname.exists()
    # A new Database instance on the same file continues with the recorded versions:
    db2 = Database(change_tg, fname=fname)
    assert db2.apply_changes(
        [Change('parent', 'p1', 1, {'id': 'p1', 'label': 'one'})]).skipped == 1
    assert [r['label'] for r in read_rows(db2)['parent']] == ['one']


def test_apply_changes_non_text_list_values():
    tg = TableGroup.fromvalue({'tables': [
        {'url': 't', 'tableSchema': {
            'columns': [
                {'name': 'id'},
                {'name': 'ints', 'separator': ';', 'datatype': 'integer'},
            ],
            'primaryKey': ['id'],
        }},
    ]})
    db = Database(tg)
    db.apply_changes(Change('t', 'x', 1, {'id': 'x', 'ints': [1, 2, 3]}))
    assert read_rows(db)['t'][0]['ints'] == [1, 2, 3]
    db.apply_changes(Change('t', 'x', 2, {'id': 'x', 'ints': None}))
    assert read_rows(db)['t'][0]['ints'] is None
    db._connection.close()


def test_apply_changes_custom_association_context(tg_with_foreign_keys):
    class DatabaseWithCustomContext(Database):
        def association_table_context(self, table, column, fkey):
            # A custom context, but still distinguishing the two list-valued columns:
            return fkey, f'ctx:{column}'

        def select_many_to_many(self, db, table, context):
            refs = Database.select_many_to_many(self, db, table, None)
            return {
                key: [(value, ctx) for value, ctx in values if ctx == f'ctx:{context}']
                for key, values in refs.items()}

    db = DatabaseWithCustomContext(tg_with_foreign_keys)
    db.apply_changes([
        Change('data', 'x', 1, {'v': 'x'}),
        Change('data', 'y', 1, {'v': 'y'}),
        Change('ref', '1', 1, {'pk': '1', 'ref1': ['y', 'x'], 'ref2': ['x']}),
    ])
    row = read_rows(db)['ref'][0]
    assert row['ref1'] == [('y', 'ctx:ref1'), ('x', 'ctx:ref1')]
    assert row['ref2'] == [('x', 'ctx:ref2')]
    # Updating only `ref1` replaces only that column's associations:
    db.apply_changes(Change('ref', '1', 2, {'pk': '1', 'ref1': ['x'], 'ref2': ['y']}))
    row = read_rows(db)['ref'][0]
    assert row['ref1'] == [('x', 'ctx:ref1')]
    assert row['ref2'] == [('y', 'ctx:ref2')]
    db._connection.close()


def test_apply_changes_after_write(tg_with_foreign_keys):
    db = Database(tg_with_foreign_keys)
    db.write(ref=[{'pk': '1', 'ref1': ['x']}], data=[{'v': 'x'}, {'v': 'y'}])
    # Existing data has no recorded version yet:
    assert db.read_versions() == {}
    db.apply_changes([
        Change('ref', '1', 1, {'pk': '1', 'ref1': ['y'], 'ref2': []}),
    ])
    assert read_rows(db)['ref'][0]['ref1'] == ['y']
    assert db.read_versions() == {('ref', ('1',)): 1}
    db._connection.close()


def test_apply_changes_empty_batch(change_tg):
    db = Database(change_tg)
    assert db.apply_changes([]) == ApplyResult()
    assert db.apply_changes() == ApplyResult()
    db.apply_changes(Change('parent', 'p1', 1, {'id': 'p1', 'label': 'one'}))
    db._connection.close()


@pytest.mark.parametrize('drop_self_referential_fks', [True, False])
def test_apply_changes_self_referential_foreign_keys(drop_self_referential_fks):
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'nodes', 'tableSchema': {
            'columns': [{'name': 'id'}, {'name': 'parent'}],
            'primaryKey': ['id'],
            'foreignKeys': [{
                'columnReference': ['parent'],
                'reference': {'resource': 'nodes', 'columnReference': ['id']},
            }],
        }},
    ]})
    db = Database(tg, drop_self_referential_fks=drop_self_referential_fks)
    db.apply_changes([
        Change('nodes', 'b', 1, {'id': 'b', 'parent': 'a'}),
        Change('nodes', 'a', 1, {'id': 'a', 'parent': None}),
    ])
    # Deleting a referenced node is rejected, regardless of the SQL schema:
    with pytest.raises(ForeignKeyViolation):
        db.apply_changes([Change('nodes', 'a', 2, delete=True)])
    assert sorted(r['id'] for r in read_rows(db)['nodes']) == ['a', 'b']
    db._connection.close()


def test_apply_changes_composite_foreign_key():
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'parent', 'tableSchema': {
            'columns': [{'name': 'a'}, {'name': 'b'}], 'primaryKey': ['a', 'b']}},
        {'url': 'child', 'tableSchema': {
            'columns': [{'name': 'id'}, {'name': 'pa'}, {'name': 'pb'}],
            'primaryKey': ['id'],
            'foreignKeys': [{
                'columnReference': ['pa', 'pb'],
                'reference': {'resource': 'parent', 'columnReference': ['a', 'b']},
            }],
        }},
    ]})
    db = Database(tg)
    db.apply_changes([
        Change('child', 'c1', 1, {'id': 'c1', 'pa': 'x', 'pb': 'y'}),
        Change('parent', ('x', 'y'), 1, {'a': 'x', 'b': 'y'}),
    ])
    with pytest.raises(ForeignKeyViolation):
        db.apply_changes([Change('child', 'c1', 2, {'id': 'c1', 'pa': 'x', 'pb': 'z'})])
    # A null-valued composite foreign key is allowed:
    db.apply_changes([Change('child', 'c1', 2, {'id': 'c1', 'pa': None, 'pb': None})])
    db._connection.close()
