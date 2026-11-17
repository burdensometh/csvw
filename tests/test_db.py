import time
import decimal
import pathlib
import sqlite3
import warnings
from datetime import date

import pytest

from csvw import TableGroup, Column, Table, ForeignKey
from csvw.metadata import DATATYPES
from csvw.db import Database

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


def _decimal_tg():
    return TableGroup.fromvalue({'tables': [{'url': 'data', 'tableSchema': {'columns': [
        {'name': 'grp', 'datatype': 'string'},
        {'name': 'amount', 'datatype': 'decimal'},
        {'name': 'num', 'datatype': 'integer'},
    ]}}]})


def test_legacy_decimal_mode_keeps_real_storage(tg):
    tg.tables[0].tableSchema.columns.append(
        Column.fromvalue({'name': 'v', 'datatype': 'decimal'}))
    db = Database(tg)
    assert db.tdict['data'].columns[0].db_type.name == 'REAL'
    db.write(data=[{'v': decimal.Decimal('0.1')}])
    with db.connection() as conn:
        assert conn.execute('SELECT typeof(v) FROM data').fetchone() == ('real',)
    db._connection.close()


def test_exact_decimals_stored_as_canonical_text():
    tg = _decimal_tg()
    db = Database(tg, exact_decimals=True)
    col = db.tdict['data'].columns[1]
    assert col.db_type.name == 'TEXT'
    assert 'COLLATE csvw_decimal' in col.sql(db.translate)
    # Non-decimal columns keep their ordinary storage type.
    assert db.tdict['data'].columns[2].db_type.name == 'INTEGER'

    values = [
        decimal.Decimal('1.2300'), decimal.Decimal('-2.5'), decimal.Decimal('100'),
        decimal.Decimal('0.001'), decimal.Decimal('9.99'), None, decimal.Decimal('1.23'),
    ]
    db.write(data=[{'grp': 'a', 'amount': v, 'num': i} for i, v in enumerate(values)])
    with db.connection() as conn:
        assert conn.execute(
            "SELECT DISTINCT typeof(amount) FROM data WHERE amount IS NOT NULL").fetchall() \
            == [('text',)]
        # Trailing zeros are preserved verbatim and no float representation appears.
        assert conn.execute(
            "SELECT amount FROM data WHERE num = 0").fetchone() == ('1.2300',)
    db._connection.close()


def test_exact_decimals_order_by_numeric_value_not_text():
    # A purely lexicographic ordering would place '100' before '9.99'; numeric must not.
    tg = _decimal_tg()
    db = Database(tg, exact_decimals=True)
    db.write(data=[
        {'grp': 'a', 'amount': decimal.Decimal(v), 'num': 1}
        for v in ['100', '-2.5', '9.99', '0.001', '-100.0', '10.50']])
    ordered = [r[0] for r in db.query('SELECT amount FROM data ORDER BY amount')]
    assert ordered == ['-100.0', '-2.5', '0.001', '9.99', '10.50', '100']
    descending = [r[0] for r in db.query('SELECT amount FROM data ORDER BY amount DESC')]
    assert descending == list(reversed(ordered))
    # Filtering compares numerically as well (text comparison would misjudge '100' vs '9.99').
    assert [r[0] for r in db.query(
        "SELECT amount FROM data WHERE amount > '9.99' ORDER BY amount")] == \
        ['10.50', '100']
    # A Decimal parameter is adapted to exact text rather than bound as float.
    assert [r[0] for r in db.query(
        'SELECT amount FROM data WHERE amount > ? ORDER BY amount',
        [decimal.Decimal('9.99')])] == ['10.50', '100']
    db._connection.close()


def test_exact_decimals_grouping_and_distinct_are_numeric():
    tg = _decimal_tg()
    db = Database(tg, exact_decimals=True)
    db.write(data=[
        {'grp': 'a', 'amount': None if v is None else decimal.Decimal(v), 'num': 1}
        for v in ['1.0', '1.00', '1', '2.50', '2.5', None]])
    # Numerically equal representations group together; NULL stays its own group.
    groups = db.query(
        'SELECT amount, COUNT(*) FROM data GROUP BY amount ORDER BY amount')
    assert groups == [(None, 1), ('1.0', 3), ('2.50', 2)]
    assert [r[0] for r in db.query('SELECT DISTINCT amount FROM data ORDER BY amount')] == \
        [None, '1.0', '2.50']
    db._connection.close()


def test_exact_decimal_sum_avoids_float_and_context_truncation():
    tg = _decimal_tg()
    # Each addend has 30 fractional digits; the exact sum cannot fit in the default
    # 28-digit Decimal context nor in a binary float.
    parts = [decimal.Decimal('0.123456789012345678901234567891') for _ in range(10)]
    db = Database(tg, exact_decimals=True)
    db.write(data=[{'grp': 'a', 'amount': v, 'num': 1} for v in parts] +
                  [{'grp': 'b', 'amount': None, 'num': 1}])
    total = decimal.Decimal(
        db.query('SELECT csvw_decimal_sum(amount) FROM data')[0][0])
    with decimal.localcontext(decimal.Context(prec=60)):
        assert total == sum(parts, decimal.Decimal(0))
    # Aggregate over an empty / all-NULL set returns SQL NULL, like SQLite's sum().
    assert db.query(
        "SELECT csvw_decimal_sum(amount) FROM data WHERE grp = 'b'")[0][0] is None
    # Grouped summation stays exact and numeric.
    grouped = db.query(
        "SELECT grp, csvw_decimal_sum(amount) FROM data GROUP BY grp ORDER BY grp")
    with decimal.localcontext(decimal.Context(prec=60)):
        expected = format(sum(parts, decimal.Decimal(0)), 'f')
    assert grouped[0] == ('a', expected)
    assert grouped[1][0] == 'b' and grouped[1][1] is None
    db._connection.close()


def test_exact_decimals_support_negative_numbers_and_null(tg):
    tg.tables[0].tableSchema.columns.append(
        Column.fromvalue({'name': 'v', 'datatype': 'decimal'}))
    db = Database(tg, exact_decimals=True)
    values = [decimal.Decimal('-0.0001'), decimal.Decimal('-1000000.5'), None,
              decimal.Decimal('0'), decimal.Decimal('+7.25')]
    db.write(data=[{'v': v} for v in values])
    assert [r['v'] for r in db.read()['data']] == values
    assert db.query('SELECT min(v), max(v) FROM data')[0] == ('-1000000.5', '7.25')
    # -0 and 0 compare equal under the numeric collation.
    with db.connection() as conn:
        conn.execute('DELETE FROM data')
    db.write(data=[{'v': decimal.Decimal('0')}, {'v': decimal.Decimal('-0.0')}])
    assert db.query(
        "SELECT COUNT(*) FROM data WHERE v = '0'")[0][0] == 2
    db._connection.close()


def test_exact_decimals_round_trip_back_to_csv(tg, tmp_path):
    import csv as _csv
    import io
    col = Column.fromvalue({'name': 'v', 'datatype': 'decimal'})
    tg.tables[0].tableSchema.columns.append(col)
    db = Database(tg, exact_decimals=True)
    long_value = decimal.Decimal('100.0000000000000000000000000001')
    db.write(data=[{'v': long_value}])
    # The long decimal is stored verbatim and re-exported byte-for-byte, never passing
    # through binary float (note: the csvw cell *parser* itself applies the default Decimal
    # context precision when reading such strings, which is independent of DB storage).
    csv_text = tg.tables[0].write(db.read()['data'], fname=None).decode('utf-8')
    assert '100.0000000000000000000000000001' in csv_text

    # Full read/write/read round-trip for values within the parser's Decimal context.
    values = [decimal.Decimal('1.2300'), decimal.Decimal('-2.5'), None,
              decimal.Decimal('0.0000000000000000000000000001')]
    db2 = Database(tg, exact_decimals=True)
    db2.write(data=[{'v': v} for v in values])
    rows = db2.read()['data']
    csv_text = tg.tables[0].write(rows, fname=None).decode('utf-8')
    parsed = list(_csv.reader(io.StringIO(csv_text)))
    assert parsed == [
        ['v'], ['1.2300'], ['-2.5'], [''],
        ['0.0000000000000000000000000001']]
    reread = [col.read(line[0]) for line in parsed[1:]]
    assert reread == values
    db._connection.close()
    db2._connection.close()


def test_exact_decimals_reject_float_and_non_finite_input(tg):
    tg.tables[0].tableSchema.columns.append(
        Column.fromvalue({'name': 'v', 'datatype': 'decimal'}))
    db = Database(tg, exact_decimals=True)
    with pytest.raises(TypeError, match='float'):
        db.write(data=[{'v': 1.5}])
    with pytest.raises(ValueError, match='finite'):
        db.write(data=[{'v': decimal.Decimal('NaN')}])
    with pytest.raises(ValueError, match='finite'):
        db.write(data=[{'v': decimal.Decimal('Infinity')}])
    db._connection.close()


def test_exact_decimals_accept_int_and_str_input(tg):
    tg.tables[0].tableSchema.columns.append(
        Column.fromvalue({'name': 'v', 'datatype': 'decimal'}))
    db = Database(tg, exact_decimals=True)
    db.write(data=[{'v': 5}, {'v': '007.50'}, {'v': None}])
    with db.connection() as conn:
        assert [r[0] for r in conn.execute('SELECT v FROM data')] == ['5', '7.50', None]
    db._connection.close()


def test_exact_decimal_check_constraints(tg):
    tg.tables[0].tableSchema.columns.append(Column.fromvalue(
        {'name': 'v', 'datatype': {'base': 'decimal', 'minimum': -90, 'maximum': 90}}))
    db = Database(tg, exact_decimals=True)
    db.write(data=[{'v': decimal.Decimal('-10.5')}, {'v': decimal.Decimal('90.00')}])
    for bad in [decimal.Decimal('100'), decimal.Decimal('-90.001')]:
        with pytest.raises(sqlite3.IntegrityError):
            db.write(data=[{'v': bad}])
    db._connection.close()


def test_exact_decimals_persist_to_file_and_reread(tg, tmp_path):
    tg.tables[0].tableSchema.columns.append(
        Column.fromvalue({'name': 'v', 'datatype': 'decimal'}))
    fname = tmp_path / 'exact.sqlite'
    db = Database(tg, fname=fname, exact_decimals=True)
    db.write(data=[{'v': decimal.Decimal('1.2345678901234567890123456789')}])

    # A fresh Database on the same file re-registers collation and aggregate functions.
    db2 = Database(tg, fname=fname, exact_decimals=True)
    assert db2.read()['data'][0]['v'] == \
        decimal.Decimal('1.2345678901234567890123456789')
    assert db2.query('SELECT csvw_decimal_sum(v) FROM data')[0][0] == \
        '1.2345678901234567890123456789'

    # Without exact mode the file is still openable, but the text is read via the legacy
    # REAL-based converter path only for Database(tg) instances; here we simply confirm the
    # on-disk representation is plain text.
    raw = sqlite3.connect(fname)
    assert raw.execute('SELECT typeof(v), v FROM data').fetchone() == \
        ('text', '1.2345678901234567890123456789')
    raw.close()


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
