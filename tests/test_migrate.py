import json
import argparse
import sqlite3
import zipfile

import pytest

from csvw import TableGroup, Table, CSVW
from csvw.db import Database
from csvw.migrate import rename_columns, RenameError


def write_dataset(tmp_path, metadata, files):
    for name, content in files.items():
        tmp_path.joinpath(name).write_text(content, encoding='utf-8')
    md = tmp_path / 'metadata.json'
    md.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    return md


def group_metadata(**kw):
    md = {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [
            {
                'url': 'parents.csv',
                'tableSchema': {
                    'columns': [
                        {'name': 'ID', 'datatype': 'string'},
                        {'name': 'name', 'datatype': 'string'},
                    ],
                    'primaryKey': ['ID'],
                },
            },
            {
                'url': 'children.csv',
                'tableSchema': {
                    'columns': [
                        {'name': 'ID', 'datatype': 'integer'},
                        {'name': 'PARENT', 'datatype': 'string'},
                        {'name': 'about', 'datatype': 'string'},
                    ],
                    'primaryKey': ['ID'],
                    'foreignKeys': [
                        {'columnReference': ['PARENT'],
                         'reference': {'resource': 'parents.csv', 'columnReference': ['ID']}}
                    ],
                },
            },
        ],
    }
    md.update(kw)
    return md


GROUP_FILES = {
    'parents.csv': 'ID,name\np1,Parent One\np2,Parent Two\n',
    'children.csv': 'ID,PARENT,about\n1,p1,a\n2,p2,b\n',
}


@pytest.fixture
def group(tmp_path):
    md = write_dataset(tmp_path, group_metadata(), GROUP_FILES)
    return md, TableGroup.from_file(md)


def test_rename_roundtrip(group, tmp_path):
    md, tg = group
    report = rename_columns(tg, {
        'parents.csv': {'ID': 'id'},
        'children.csv': {'ID': 'id', 'PARENT': 'parent_id'},
    })
    assert {(r.table, r.old, r.new) for r in report.renames} == {
        ('parents.csv', 'ID', 'id'),
        ('children.csv', 'ID', 'id'),
        ('children.csv', 'PARENT', 'parent_id'),
    }
    assert set(report.csv_files) == {tmp_path / 'parents.csv', tmp_path / 'children.csv'}
    assert report.metadata_file == md
    assert not report.dry_run

    # Schema, primary keys and both ends of the foreign key are updated:
    parents, children = tg.tabledict['parents.csv'], tg.tabledict['children.csv']
    assert [c.name for c in parents.tableSchema.columns] == ['id', 'name']
    assert parents.tableSchema.primaryKey == ['id']
    assert [c.name for c in children.tableSchema.columns] == ['id', 'parent_id', 'about']
    fk = children.tableSchema.foreignKeys[0]
    assert fk.columnReference == ['parent_id']
    assert fk.reference.columnReference == ['id']
    assert fk.reference.resource.string == 'parents.csv'

    # CSV headers are rewritten, all row values are preserved:
    assert tmp_path.joinpath('parents.csv').read_text(encoding='utf-8') == \
        'id,name\np1,Parent One\np2,Parent Two\n'
    assert tmp_path.joinpath('children.csv').read_text(encoding='utf-8') == \
        'id,parent_id,about\n1,p1,a\n2,p2,b\n'

    # The metadata file reflects the changes:
    data = json.loads(md.read_text(encoding='utf-8'))
    t1, t2 = data['tables']
    assert [c['name'] for c in t1['tableSchema']['columns']] == ['id', 'name']
    assert t1['tableSchema']['primaryKey'] == ['id']
    assert t2['tableSchema']['foreignKeys'][0]['columnReference'] == ['parent_id']
    assert t2['tableSchema']['foreignKeys'][0]['reference']['columnReference'] == ['id']

    # The migrated dataset can be read (typed), validated and loaded into SQLite:
    tg = TableGroup.from_file(md)
    rows = list(tg.tabledict['children.csv'].iterdicts())
    assert rows[0] == {'id': 1, 'parent_id': 'p1', 'about': 'a'}
    assert isinstance(rows[0]['id'], int)
    assert tg.check_referential_integrity()
    assert CSVW(str(md)).is_valid

    db = Database(tg, fname=tmp_path / 'test.db')
    db.write_from_tg()
    with sqlite3.connect(str(tmp_path / 'test.db')) as conn:
        assert conn.execute('SELECT * FROM `children.csv` ORDER BY id').fetchall() == [
            (1, 'p1', 'a'), (2, 'p2', 'b')]
        # Foreign keys are enforced:
        conn.execute('PRAGMA foreign_keys = ON')
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO `children.csv` VALUES (3, 'p3', 'x')")


def test_rename_swap_and_chain(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [
                {'name': 'A', 'datatype': 'integer'},
                {'name': 'B', 'datatype': 'string'},
                {'name': 'C', 'datatype': 'string'},
            ]},
        }],
    }, {'data.csv': 'A,B,C\n1,x,y\n'})
    tg = TableGroup.from_file(md)
    # Swapping names in one batch works, because renames are applied by column
    # identity: datatypes and values stay with their columns.
    rename_columns(tg, {'data.csv': {'A': 'B', 'B': 'A'}})
    assert [c.name for c in tg.tables[0].tableSchema.columns] == ['B', 'A', 'C']
    assert tmp_path.joinpath('data.csv').read_text(encoding='utf-8') == 'B,A,C\n1,x,y\n'
    tg = TableGroup.from_file(md)
    row = list(tg.tables[0].iterdicts())[0]
    assert row['B'] == 1 and isinstance(row['B'], int)  # the column formerly known as A
    assert row['A'] == 'x'
    # Chaining names in one batch works, too:
    rename_columns(tg, {'data.csv': {'A': 'C', 'C': 'D'}})
    assert [c.name for c in tg.tables[0].tableSchema.columns] == ['B', 'C', 'D']


def test_row_titles_and_uri_templates(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {
                'columns': [
                    {'name': 'ID', 'datatype': 'string'},
                    {'name': 'name', 'datatype': 'string',
                     'valueUrl': 'http://example.org/{ID}/{name}'},
                ],
                'primaryKey': ['ID'],
                'rowTitles': ['ID', 'name'],
                'aboutUrl': 'http://example.org/ID/{ID}{?name}',
                'propertyUrl': 'http://example.org/prop{#ID}',
            },
        }],
    }, {'data.csv': 'ID,name\np1,Parent One\n'})
    tg = TableGroup.from_file(md)
    report = rename_columns(tg, {'data.csv': {'ID': 'id'}})
    schema = tg.tables[0].tableSchema
    assert schema.rowTitles == ['id', 'name']
    # Only template variables are renamed - literal occurrences of the old name
    # outside template expressions are left untouched:
    assert schema.aboutUrl == 'http://example.org/ID/{id}{?name}'
    assert schema.propertyUrl == 'http://example.org/prop{#id}'
    assert schema.columns[1].valueUrl == 'http://example.org/{id}/{name}'
    assert any('rowTitles' in r for r in report.references)
    assert any('aboutUrl' in r for r in report.references)
    # The metadata file contains the updated templates:
    data = json.loads(md.read_text(encoding='utf-8'))
    schema = data['tables'][0]['tableSchema']
    assert schema['rowTitles'] == ['id', 'name']
    assert schema['aboutUrl'] == 'http://example.org/ID/{id}{?name}'
    # ... and the data can still be read:
    tg = TableGroup.from_file(md)
    assert list(tg.tables[0].iterdicts())[0] == {'id': 'p1', 'name': 'Parent One'}


def test_uri_template_modifiers(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {
                'columns': [{'name': 'ID'}, {'name': 'q'}],
                'aboutUrl': '{+ID}/{ID:3}{?ID,q}{&q}{#ID*}{?ID=x}',
            },
        }],
    }, {'data.csv': 'ID,q\np1,x\n'})
    tg = TableGroup.from_file(md)
    rename_columns(tg, {'data.csv': {'ID': 'id'}})
    # Operators, modifiers and defaults are preserved:
    assert tg.tables[0].tableSchema.aboutUrl == '{+id}/{id:3}{?id,q}{&q}{#id*}{?id=x}'


def test_uri_template_chained_renames(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {
                'columns': [{'name': 'A'}, {'name': 'B'}, {'name': 'C'}],
                'aboutUrl': 'http://example.org/{A}/{B}{?A,B}',
            },
        }],
    }, {'data.csv': 'A,B,C\n1,2,3\n'})
    tg = TableGroup.from_file(md)
    # Template variables are renamed simultaneously, so chains and swaps work:
    rename_columns(tg, {'data.csv': {'A': 'B', 'B': 'C', 'C': 'D'}})
    assert tg.tables[0].tableSchema.aboutUrl == 'http://example.org/{B}/{C}{?B,C}'
    rename_columns(tg, {'data.csv': {'B': 'C', 'C': 'B'}})
    assert tg.tables[0].tableSchema.aboutUrl == 'http://example.org/{C}/{B}{?C,B}'


def test_uri_template_invalid_new_variable_name(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {
                'columns': [{'name': 'ID'}, {'name': 'name'}],
                'aboutUrl': 'http://example.org/{ID}',
            },
        }],
    }, {'data.csv': 'ID,name\n1,x\n'})
    tg = TableGroup.from_file(md)
    before = tmp_path.joinpath('data.csv').read_bytes()
    # A comma has structural meaning in URI template expressions:
    with pytest.raises(RenameError, match='cannot be used as variable'):
        rename_columns(tg, {'data.csv': {'ID': 'a,b'}})
    assert tmp_path.joinpath('data.csv').read_bytes() == before
    assert [c.name for c in tg.tables[0].tableSchema.columns] == ['ID', 'name']


def test_group_level_template_error(tmp_path):
    md = write_dataset(tmp_path, group_metadata(
        aboutUrl='http://example.org/{ID}'), GROUP_FILES)
    tg = TableGroup.from_file(md)
    with pytest.raises(RenameError, match='TableGroup'):
        rename_columns(tg, {'parents.csv': {'ID': 'id'}})


def test_conflicts_and_broken_references(group, tmp_path):
    md, tg = group
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    # Renaming to an existing column name:
    with pytest.raises(RenameError, match="2 columns named 'name'"):
        rename_columns(tg, {'parents.csv': {'ID': 'name'}})
    # Two renames to the same target:
    with pytest.raises(RenameError, match='columns named'):
        rename_columns(tg, {'children.csv': {'ID': 'x', 'PARENT': 'x'}})
    # Unknown column, unknown table, invalid new names:
    with pytest.raises(RenameError, match="no column named 'NOPE'"):
        rename_columns(tg, {'parents.csv': {'NOPE': 'x'}})
    with pytest.raises(RenameError, match="unknown table 'nope.csv'"):
        rename_columns(tg, {'nope.csv': {'ID': 'id'}})
    with pytest.raises(RenameError, match='invalid new column name'):
        rename_columns(tg, {'parents.csv': {'ID': 'has space'}})
    with pytest.raises(RenameError, match='invalid new column name'):
        rename_columns(tg, {'parents.csv': {'ID': '_hidden'}})
    with pytest.raises(RenameError, match='invalid new column name'):
        rename_columns(tg, {'parents.csv': {'ID': ''}})
    # A flat mapping is not accepted for a TableGroup:
    with pytest.raises(RenameError, match='unknown table'):
        rename_columns(tg, {'ID': 'id'})
    # Nothing was changed:
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert [c.name for c in tg.tabledict['parents.csv'].tableSchema.columns] == ['ID', 'name']


def test_noop_rename(group):
    md, tg = group
    report = rename_columns(tg, {'parents.csv': {'ID': 'ID'}})
    assert report.renames == []
    assert report.csv_files == []
    assert report.metadata_file is None


def test_preexisting_broken_references(tmp_path):
    md = write_dataset(tmp_path, group_metadata(), GROUP_FILES)
    data = json.loads(md.read_text(encoding='utf-8'))
    data['tables'][0]['tableSchema']['rowTitles'] = ['NOPE']
    data['tables'][1]['tableSchema']['foreignKeys'][0]['reference']['columnReference'] = ['NOPE']
    md.write_text(json.dumps(data), encoding='utf-8')
    tg = TableGroup.from_file(md)
    with pytest.raises(RenameError, match='would not resolve'):
        rename_columns(tg, {'children.csv': {'about': 'comment'}})


def test_preexisting_broken_references_2(tmp_path):
    md = write_dataset(tmp_path, group_metadata(), GROUP_FILES)
    data = json.loads(md.read_text(encoding='utf-8'))
    data['tables'][0]['tableSchema']['primaryKey'] = ['NOPE']
    data['tables'][1]['tableSchema']['foreignKeys'][0]['columnReference'] = ['NOPE']
    md.write_text(json.dumps(data), encoding='utf-8')
    tg = TableGroup.from_file(md)
    with pytest.raises(RenameError, match='would not resolve'):
        rename_columns(tg, {'children.csv': {'about': 'comment'}})


def test_invalid_rename_specs(group):
    md, tg = group
    with pytest.raises(RenameError, match='must be specified as dict'):
        rename_columns(tg, ['ID', 'id'])
    with pytest.raises(RenameError, match='Invalid renames for table'):
        rename_columns(tg, {'parents.csv': ['ID']})
    with pytest.raises(RenameError, match='Invalid renames for table'):
        rename_columns(tg, {'parents.csv': {'ID': 5}})
    with pytest.raises(TypeError, match='requires a TableGroup or Table'):
        rename_columns('parents.csv', {'ID': 'id'})


def test_collision_with_unnamed_column_header(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [{'name': 'ID'}, {'titles': ['Label']}]},
        }],
    }, {'data.csv': 'ID,Label\n1,x\n'})
    tg = TableGroup.from_file(md)
    with pytest.raises(RenameError, match='unnamed column'):
        rename_columns(tg, {'data.csv': {'ID': 'Label'}})


def test_csv_header_conflict(tmp_path):
    # The CSV file has a column which is not described in the schema:
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [{'name': 'ID'}, {'name': 'name'}]},
        }],
    }, {'data.csv': 'ID,name,extra\n1,x,y\n'})
    tg = TableGroup.from_file(md)
    with pytest.raises(RenameError, match='duplicate a header cell'):
        rename_columns(tg, {'data.csv': {'ID': 'extra'}})


def test_column_not_in_csv_header(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [{'name': 'ID'}, {'name': 'name'}]},
        }],
    }, {'data.csv': 'X,name\n1,x\n'})
    tg = TableGroup.from_file(md)
    with pytest.raises(RenameError, match='not found in CSV header'):
        rename_columns(tg, {'data.csv': {'ID': 'id'}})


def test_ambiguous_csv_header(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [
                {'name': 'ID', 'titles': ['label']}, {'name': 'name'}]},
        }],
    }, {'data.csv': 'label,label\n1,x\n'})
    tg = TableGroup.from_file(md)
    with pytest.raises(RenameError, match='multiple cells'):
        rename_columns(tg, {'data.csv': {'ID': 'id'}})


def test_csv_header_cell_double_claim(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [
                {'name': 'A', 'titles': ['x']}, {'name': 'B', 'titles': ['x']}]},
        }],
    }, {'data.csv': 'x,y\n1,2\n'})
    tg = TableGroup.from_file(md)
    with pytest.raises(RenameError, match='same cell'):
        rename_columns(tg, {'data.csv': {'A': 'p', 'B': 'q'}})


def test_dialect_and_file_content_preserved(tmp_path):
    content = '# a comment\nID;name\n1;"a; b"\n2;"c"\n'
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'dialect': {'delimiter': ';', 'commentPrefix': '#'},
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [{'name': 'ID'}, {'name': 'name'}]},
        }],
    }, {'data.csv': content})
    tg = TableGroup.from_file(md)
    rename_columns(tg, {'data.csv': {'ID': 'id'}})
    # Only the header cell changed; comments, quoting and row values are intact:
    assert tmp_path.joinpath('data.csv').read_text(encoding='utf-8') == \
        '# a comment\nid;name\n1;"a; b"\n2;"c"\n'
    tg = TableGroup.from_file(md)
    assert list(tg.tables[0].iterdicts())[1] == {'id': '2', 'name': 'c'}
    assert tg.dialect.delimiter == ';'


def test_bom_preserved(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [{'name': 'ID'}, {'name': 'name'}]},
        }],
    }, {})
    tmp_path.joinpath('data.csv').write_bytes('ID,name\n1,x\n'.encode('utf-8-sig'))
    tg = TableGroup.from_file(md)
    rename_columns(tg, {'data.csv': {'ID': 'id'}})
    raw = tmp_path.joinpath('data.csv').read_bytes()
    assert raw.startswith(b'\xef\xbb\xbf')
    assert raw.decode('utf-8-sig') == 'id,name\n1,x\n'


def test_encoding_preserved(tmp_path):
    for encoding, raw, expected in [
            # Non-utf-8 encoding:
            ('latin1', 'ID,name\n1,ä\n'.encode('latin1'), 'id,name\n1,ä\n'.encode('latin1')),
            # Declared with BOM, but data has none - so none must be added:
            ('utf-8-sig', b'ID,name\n1,x\n', b'id,name\n1,x\n')]:
        md = write_dataset(tmp_path, {
            '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
            'dialect': {'encoding': encoding},
            'tables': [{
                'url': 'data.csv',
                'tableSchema': {'columns': [{'name': 'ID'}, {'name': 'name'}]},
            }],
        }, {})
        tmp_path.joinpath('data.csv').write_bytes(raw)
        tg = TableGroup.from_file(md)
        rename_columns(tg, {'data.csv': {'ID': 'id'}})
        assert tmp_path.joinpath('data.csv').read_bytes() == expected
        tg = TableGroup.from_file(md)
        assert list(tg.tables[0].iterdicts())[0]['id'] == '1'


def test_undecodable_csv_error(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'dialect': {'encoding': 'ascii'},
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [{'name': 'ID'}, {'name': 'name'}]},
        }],
    }, {})
    tmp_path.joinpath('data.csv').write_bytes(b'ID,name\n1,\xff\n')
    tg = TableGroup.from_file(md)
    with pytest.raises(RenameError, match='cannot decode'):
        rename_columns(tg, {'data.csv': {'ID': 'id'}})


def test_malformed_csv_error(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [{'name': 'ID'}, {'name': 'name'}]},
        }],
    }, {'data.csv': 'ID,"name\n1,x\n'})  # unterminated quoted field
    tg = TableGroup.from_file(md)
    with pytest.raises(RenameError, match='cannot parse'):
        rename_columns(tg, {'data.csv': {'ID': 'id'}})


def test_empty_csv_error(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [{'name': 'ID'}, {'name': 'name'}]},
        }],
    }, {'data.csv': ''})
    tg = TableGroup.from_file(md)
    with pytest.raises(RenameError, match='no header row'):
        rename_columns(tg, {'data.csv': {'ID': 'id'}})


def test_no_trailing_newline(tmp_path):
    for content, expected in [
            ('ID,name', 'id,name'),  # header only, no trailing newline
            ('ID,name\n1,x', 'id,name\n1,x')]:  # no trailing newline on last data row
        md = write_dataset(tmp_path, {
            '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
            'tables': [{
                'url': 'data.csv',
                'tableSchema': {'columns': [{'name': 'ID'}, {'name': 'name'}]},
            }],
        }, {'data.csv': content})
        tg = TableGroup.from_file(md)
        rename_columns(tg, {'data.csv': {'ID': 'id'}})
        assert tmp_path.joinpath('data.csv').read_text(encoding='utf-8') == expected


def test_header_with_titles(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [
                {'name': 'ID', 'titles': ['Identifier']}, {'name': 'name'}]},
        }],
    }, {'data.csv': 'Identifier,name\n1,x\n'})
    tg = TableGroup.from_file(md)
    rename_columns(tg, {'data.csv': {'ID': 'id'}})
    assert tmp_path.joinpath('data.csv').read_text(encoding='utf-8') == 'id,name\n1,x\n'
    tg = TableGroup.from_file(md)
    assert list(tg.tables[0].iterdicts())[0] == {'id': '1', 'name': 'x'}


def test_new_name_needs_quoting(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'dialect': {'delimiter': ';'},
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [{'name': 'ID'}, {'name': 'name'}]},
        }],
    }, {'data.csv': 'ID;name\n1;x\n'})
    tg = TableGroup.from_file(md)
    rename_columns(tg, {'data.csv': {'ID': 'weird;name'}})
    assert tmp_path.joinpath('data.csv').read_text(encoding='utf-8') == '"weird;name";name\n1;x\n'
    tg = TableGroup.from_file(md)
    assert list(tg.tables[0].iterdicts())[0] == {'weird;name': '1', 'name': 'x'}


def test_header_false(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'dialect': {'header': False},
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [{'name': 'ID'}, {'name': 'name'}],
                            'primaryKey': ['ID']},
        }],
    }, {'data.csv': 'p1,x\np2,y\n'})
    tg = TableGroup.from_file(md)
    report = rename_columns(tg, {'data.csv': {'ID': 'id'}})
    # Without a header row, the data file is not touched:
    assert report.csv_files == []
    assert tmp_path.joinpath('data.csv').read_text(encoding='utf-8') == 'p1,x\np2,y\n'
    tg = TableGroup.from_file(md)
    assert tg.tables[0].tableSchema.primaryKey == ['id']
    assert list(tg.tables[0].iterdicts())[0] == {'id': 'p1', 'name': 'x'}


def test_virtual_column(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [
                {'name': 'ID'},
                {'name': 'uri', 'virtual': True, 'valueUrl': 'http://example.org/{ID}'},
            ]},
        }],
    }, {'data.csv': 'ID\np1\n'})
    tg = TableGroup.from_file(md)
    rename_columns(tg, {'data.csv': {'ID': 'id', 'uri': 'URI'}})
    schema = tg.tables[0].tableSchema
    assert [c.name for c in schema.columns] == ['id', 'URI']
    assert schema.columns[1].valueUrl == 'http://example.org/{id}'
    # Only the non-virtual column's header cell was rewritten:
    assert tmp_path.joinpath('data.csv').read_text(encoding='utf-8') == 'id\np1\n'
    tg = TableGroup.from_file(md)
    assert list(tg.tables[0].iterdicts())[0] == {'id': 'p1', 'URI': 'http://example.org/p1'}


def test_virtual_column_only_rename(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [
                {'name': 'ID'},
                {'name': 'uri', 'virtual': True, 'valueUrl': 'http://example.org/{ID}'},
            ]},
        }],
    }, {'data.csv': 'ID\np1\n'})
    tg = TableGroup.from_file(md)
    report = rename_columns(tg, {'data.csv': {'uri': 'URI'}})
    # Renaming only a virtual column does not touch the data file:
    assert report.csv_files == []
    assert tmp_path.joinpath('data.csv').read_text(encoding='utf-8') == 'ID\np1\n'
    assert [c.name for c in tg.tables[0].tableSchema.columns] == ['ID', 'URI']
    tg = TableGroup.from_file(md)
    assert list(tg.tables[0].iterdicts())[0] == {'ID': 'p1', 'URI': 'http://example.org/p1'}


def test_single_table(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'url': 'data.csv',
        'tableSchema': {'columns': [{'name': 'ID'}, {'name': 'name'}],
                        'primaryKey': ['ID']},
    }, {'data.csv': 'ID,name\n1,x\n'})
    table = Table.from_file(md)
    report = rename_columns(table, {'ID': 'id'})
    assert [c.name for c in table.tableSchema.columns] == ['id', 'name']
    assert table.tableSchema.primaryKey == ['id']
    assert report.metadata_file == md
    assert tmp_path.joinpath('data.csv').read_text(encoding='utf-8') == 'id,name\n1,x\n'
    table = Table.from_file(md)
    assert list(table.iterdicts())[0] == {'id': '1', 'name': 'x'}


def test_dry_run(group, tmp_path):
    md, tg = group
    before = {p: p.read_bytes() for p in tmp_path.iterdir()}
    report = rename_columns(
        tg, {'parents.csv': {'ID': 'id'}, 'children.csv': {'PARENT': 'parent_id'}},
        dry_run=True)
    assert report.dry_run
    assert len(report.renames) == 2
    assert set(report.csv_files) == {tmp_path / 'parents.csv', tmp_path / 'children.csv'}
    assert report.metadata_file == md
    assert any('primaryKey' in r for r in report.references)
    assert any('reference.columnReference' in r for r in report.references)
    # Nothing was changed:
    assert {p: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert tg.tabledict['parents.csv'].tableSchema.primaryKey == ['ID']


def test_zipped_csv_error(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [{'name': 'ID'}]},
        }],
    }, {})
    with zipfile.ZipFile(str(tmp_path / 'data.csv.zip'), 'w') as z:
        z.writestr('data.csv', 'ID\n1\n')
    tg = TableGroup.from_file(md)
    with pytest.raises(RenameError, match='zipped'):
        rename_columns(tg, {'data.csv': {'ID': 'id'}})


def test_remote_table_error():
    tg = TableGroup.fromvalue({'tables': [{
        'url': 'http://example.org/data.csv',
        'tableSchema': {'columns': [{'name': 'ID'}]},
    }]})
    with pytest.raises(RenameError, match='not a local file'):
        rename_columns(tg, {'http://example.org/data.csv': {'ID': 'id'}})


def test_missing_csv_error(tmp_path):
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [{'name': 'ID'}]},
        }],
    }, {})
    tg = TableGroup.from_file(md)
    with pytest.raises(RenameError, match='does not exist'):
        rename_columns(tg, {'data.csv': {'ID': 'id'}})


def test_fk_reference_by_title(tmp_path):
    # Foreign key references may also resolve via column titles:
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [
            {'url': 'parents.csv', 'tableSchema': {
                'columns': [{'name': 'ID', 'titles': ['Identifier']}, {'name': 'name'}],
                'primaryKey': ['ID']}},
            {'url': 'children.csv', 'tableSchema': {
                'columns': [{'name': 'ID'}, {'name': 'PARENT'}],
                'foreignKeys': [{'columnReference': ['PARENT'],
                                 'reference': {'resource': 'parents.csv',
                                               'columnReference': ['Identifier']}}]}},
        ],
    }, {'parents.csv': 'ID,name\np1,x\n', 'children.csv': 'ID,PARENT\n1,p1\n'})
    tg = TableGroup.from_file(md)
    # Renaming an unrelated column works, and the title-based reference is left alone:
    rename_columns(tg, {'children.csv': {'ID': 'id'}})
    assert tg.tabledict['children.csv'].tableSchema.foreignKeys[0].reference \
        .columnReference == ['Identifier']
    # Renaming the referenced column's *name* leaves the title-based reference
    # untouched, too, because titles are not changed and it still resolves:
    rename_columns(tg, {'parents.csv': {'ID': 'id'}})
    assert tg.tabledict['children.csv'].tableSchema.foreignKeys[0].reference \
        .columnReference == ['Identifier']
    tg = TableGroup.from_file(md)
    assert tg.validate_schema()


def test_table_with_unresolved_schema(tmp_path):
    # A table whose tableSchema could not be resolved is skipped by the migration:
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [
            {'url': 'data.csv', 'tableSchema': {'columns': [{'name': 'ID'}]}},
            {'url': 'other.csv', 'tableSchema': 'schema.json'},
        ],
    }, {'data.csv': 'ID\n1\n'})
    tg = TableGroup.from_file(md)
    assert isinstance(tg.tabledict['other.csv'].tableSchema, str)
    report = rename_columns(tg, {'data.csv': {'ID': 'id'}})
    assert [c.name for c in tg.tabledict['data.csv'].tableSchema.columns] == ['id']
    assert report.csv_files == [tmp_path / 'data.csv']


def test_fk_reference_by_property_url(tmp_path):
    # Foreign key references may also resolve via column propertyUrls:
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'tables': [
            {'url': 'parents.csv', 'tableSchema': {
                'columns': [{'name': 'ID', 'propertyUrl': 'http://example.org/id'},
                            {'name': 'name'}],
                'primaryKey': ['ID']}},
            {'url': 'children.csv', 'tableSchema': {
                'columns': [{'name': 'ID'}, {'name': 'PARENT'}],
                'foreignKeys': [{'columnReference': ['PARENT'],
                                 'reference': {'resource': 'parents.csv',
                                               'columnReference': ['http://example.org/id']}}]}},
        ],
    }, {'parents.csv': 'ID,name\np1,x\n', 'children.csv': 'ID,PARENT\n1,p1\n'})
    tg = TableGroup.from_file(md)
    # The reference does not involve column names, so it is left untouched:
    rename_columns(tg, {'parents.csv': {'ID': 'id'}})
    assert tg.tabledict['children.csv'].tableSchema.foreignKeys[0].reference \
        .columnReference == ['http://example.org/id']


def test_cli(tmp_path, capsys):
    from csvw.__main__ import csvwrename
    md = write_dataset(tmp_path, group_metadata(), GROUP_FILES)
    args = argparse.Namespace(
        url=str(md),
        rename=['parents.csv:ID=id', 'children.csv:PARENT=parent_id'],
        dry_run=False)
    assert csvwrename(args, test=True) == 0
    out, _ = capsys.readouterr()
    assert 'parents.csv: ID -> id' in out
    assert 'children.csv: PARENT -> parent_id' in out
    assert json.loads(md.read_text(encoding='utf-8'))['tables'][0][
        'tableSchema']['primaryKey'] == ['id']

    # A dry run reports, but does not change anything:
    before = md.read_bytes()
    args.rename = ['children.csv:about=comment']
    args.dry_run = True
    assert csvwrename(args, test=True) == 0
    out, _ = capsys.readouterr()
    assert 'Dry run' in out
    assert md.read_bytes() == before

    # Errors are reported with a non-zero exit code:
    args.rename = ['children.csv:NOPE=x']
    args.dry_run = False
    assert csvwrename(args, test=True) == 2
    out, _ = capsys.readouterr()
    assert 'no column named' in out
    # Invalid specs are rejected:
    args.rename = ['children.csv']
    assert csvwrename(args, test=True) == 2


def test_cli_single_table(tmp_path):
    from csvw.__main__ import csvwrename
    md = write_dataset(tmp_path, {
        '@context': ['http://www.w3.org/ns/csvw', {'@language': 'en'}],
        'url': 'data.csv',
        'tableSchema': {'columns': [{'name': 'ID'}, {'name': 'name'}]},
    }, {'data.csv': 'ID,name\n1,x\n'})
    args = argparse.Namespace(url=str(md), rename=['ID=id'], dry_run=False)
    assert csvwrename(args, test=True) == 0
    assert json.loads(md.read_text(encoding='utf-8'))['tableSchema']['columns'][0][
        'name'] == 'id'
