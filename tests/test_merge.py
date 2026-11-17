"""
Tests for merging rows that describe the same entity via explicit old-key to
canonical-key mappings.
"""
import copy

import pytest

from csvw import TableGroup, MergeError, KeyMap, merge_tablegroup
from csvw.merge import check_referential_integrity
from csvw.db import Database


def make_tablegroup():
    """A table group exercising scalar, list-valued and self-referential FKs."""
    return TableGroup.fromvalue({'tables': [
        {
            'url': 'countries.csv',
            'tableSchema': {
                'columns': [
                    {'name': 'id'},
                    {'name': 'name'},
                    {'name': 'capital'},
                ],
                'primaryKey': ['id'],
            },
        },
        {
            'url': 'languages.csv',
            'tableSchema': {
                'columns': [
                    # String PK mirrors the library's own many-to-many fixtures
                    # (association-table keys are stored as text).
                    {'name': 'id'},
                    {'name': 'name'},
                    {'name': 'country'},
                    # list-valued foreign key -> association table in SQLite.
                    {'name': 'spoken_in', 'separator': ';'},
                    # A string self-referential (scalar) foreign key.
                    {'name': 'parent'},
                ],
                'primaryKey': ['id'],
                'foreignKeys': [
                    {'columnReference': 'country',
                     'reference': {'resource': 'countries.csv', 'columnReference': 'id'}},
                    {'columnReference': 'spoken_in',
                     'reference': {'resource': 'countries.csv', 'columnReference': 'id'}},
                    {'columnReference': 'parent',
                     'reference': {'resource': 'languages.csv', 'columnReference': 'id'}},
                ],
            },
        },
    ]})


def make_data():
    return {
        'countries.csv': [
            {'id': 'DE', 'name': 'Germany', 'capital': 'Berlin'},
            {'id': 'GER', 'name': None, 'capital': ''},
            {'id': 'FR', 'name': 'France', 'capital': 'Paris'},
        ],
        'languages.csv': [
            {'id': 'l1', 'name': 'German', 'country': 'DE',
             'spoken_in': ['DE', 'GER'], 'parent': None},
            {'id': 'l2', 'name': 'Deutsch', 'country': 'GER',
             'spoken_in': ['GER', 'DE', 'GER'], 'parent': 'l1'},
            {'id': 'l3', 'name': 'French', 'country': 'FR',
             'spoken_in': ['FR'], 'parent': None},
        ],
    }


# GER (old business key) is merged into canonical key DE.
COUNTRY_MERGE = {'countries.csv': {'GER': 'DE'}}


def integer_tablegroup():
    """A single table with an integer primary key for datatype parsing tests."""
    return TableGroup.fromvalue({'tables': [
        {'url': 'items.csv', 'tableSchema': {
            'columns': [{'name': 'id', 'datatype': 'integer'}, {'name': 'label'}],
            'primaryKey': ['id']}}]})


# ---------------------------------------------------------------------------
# KeyMap basics: chains, cycles, typing, composite keys
# ---------------------------------------------------------------------------

def test_keymap_resolves_chains():
    tg = make_tablegroup()
    km = KeyMap('countries.csv', {'GER': 'DE', 'DE': 'DEU', 'XX': 'GER'}).bind(
        tg.tabledict['countries.csv'])
    assert km.resolve('GER') == 'DEU'      # GER -> DE -> DEU
    assert km.resolve('DE') == 'DEU'       # DE -> DEU
    assert km.resolve('XX') == 'DEU'       # XX -> GER -> DE -> DEU
    assert km.resolve('FR') == 'FR'        # identity for unmapped keys


def test_keymap_rejects_direct_cycle():
    tg = make_tablegroup()
    with pytest.raises(MergeError, match='cyclic'):
        KeyMap('countries.csv', {'a': 'b', 'b': 'a'}).bind(
            tg.tabledict['countries.csv'])


def test_keymap_rejects_self_cycle():
    tg = make_tablegroup()
    with pytest.raises(MergeError, match='cyclic'):
        KeyMap('countries.csv', {'a': 'a'}).bind(tg.tabledict['countries.csv'])


def test_keymap_rejects_long_cycle():
    tg = make_tablegroup()
    with pytest.raises(MergeError, match='cyclic'):
        KeyMap('countries.csv', {'a': 'b', 'b': 'c', 'c': 'a'}).bind(
            tg.tabledict['countries.csv'])


def test_keymap_parses_keys_according_to_datatype():
    tg = integer_tablegroup()
    # `id` is declared integer; string keys must be normalized to ints.
    km = KeyMap('items.csv', {'2': '1', '5': 2}).bind(tg.tabledict['items.csv'])
    assert set(km._parsed.keys()) == {2, 5}
    assert km.resolve(2) == 1
    assert km.resolve(5) == 1


def test_keymap_rejects_unparseable_key():
    tg = integer_tablegroup()
    with pytest.raises(MergeError, match='cannot parse'):
        KeyMap('items.csv', {'not-an-int': 1}).bind(tg.tabledict['items.csv'])


def test_keymap_requires_primary_key():
    tg = TableGroup.fromvalue({'tables': [
        {'url': 't.csv', 'tableSchema': {'columns': [{'name': 'x'}]}}]})
    with pytest.raises(MergeError, match='primaryKey'):
        KeyMap('t.csv', {'a': 'b'}).bind(tg.tabledict['t.csv'])


def test_keymap_unknown_table():
    tg = make_tablegroup()
    with pytest.raises(MergeError, match='unknown table'):
        merge_tablegroup(tg, make_data(), {'nope.csv': {'a': 'b'}})


def test_composite_keymap_arity_check():
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'names.csv', 'tableSchema': {
            'columns': [{'name': 'lang'}, {'name': 'gloss'}, {'name': 'value'}],
            'primaryKey': ['lang', 'gloss']}}]})
    with pytest.raises(MergeError, match='composite key'):
        KeyMap('names.csv', {('a',): ('a', 'b')}).bind(tg.tabledict['names.csv'])
    km = KeyMap('names.csv', {('a', 'x'): ('a', 'y')}).bind(tg.tabledict['names.csv'])
    assert km.is_composite
    assert km.resolve(('a', 'x')) == ('a', 'y')


def test_keymap_rejects_empty_or_bad_mapping():
    with pytest.raises(MergeError, match='must not be empty'):
        KeyMap('t.csv', {})
    with pytest.raises(MergeError, match='dict'):
        KeyMap('t.csv', [('a', 'b')])


def test_keymap_rejects_duplicate_parsed_key_with_different_targets():
    tg = integer_tablegroup()
    # Both raw keys parse to the integer 2 (one is given as a string) but point
    # at different canonical targets: that is an ambiguous duplicate mapping.
    mapping = {2: 1, '2': 3}
    with pytest.raises(MergeError, match='duplicate mapping'):
        KeyMap('items.csv', mapping).bind(tg.tabledict['items.csv'])


# ---------------------------------------------------------------------------
# Merging rows: attribute conflict rules
# ---------------------------------------------------------------------------

def test_merge_combines_empty_and_equal_attributes():
    tg = make_tablegroup()
    merged = merge_tablegroup(tg, make_data(), COUNTRY_MERGE)
    countries = merged['countries.csv']
    assert len(countries) == 2
    de = next(row for row in countries if row['id'] == 'DE')
    assert de == {'id': 'DE', 'name': 'Germany', 'capital': 'Berlin'}
    # untouched rows remain:
    assert any(row['id'] == 'FR' for row in countries)


def test_merge_conflict_aborts_whole_batch():
    tg = make_tablegroup()
    data = make_data()
    data['countries.csv'][1]['name'] = 'Deutschland'  # clashes with 'Germany'
    with pytest.raises(MergeError, match='conflicting values'):
        merge_tablegroup(tg, data, COUNTRY_MERGE)
    # input data must not be mutated:
    assert data['countries.csv'][0]['id'] == 'DE'
    assert data['languages.csv'][0]['country'] == 'DE'


def test_merge_keeps_first_row_position():
    tg = make_tablegroup()
    data = {
        'countries.csv': [
            {'id': 'DE', 'name': 'Germany', 'capital': 'Berlin'},
            {'id': 'GER', 'name': None, 'capital': ''},
            {'id': 'FR', 'name': None, 'capital': None},
        ],
        'languages.csv': [],
    }
    # Merge FR into DE; the DE row is encountered first and keeps its position.
    merged = merge_tablegroup(
        tg, data, {'countries.csv': {'FR': 'DE'}}, check=False)
    assert [row['id'] for row in merged['countries.csv']] == ['DE', 'GER']


# ---------------------------------------------------------------------------
# Foreign key rewriting: scalar, composite, list-valued, indirect, self refs
# ---------------------------------------------------------------------------

def test_scalar_foreign_keys_are_rewritten():
    tg = make_tablegroup()
    merged = merge_tablegroup(tg, make_data(), COUNTRY_MERGE)
    countries = {row['id'] for row in merged['countries.csv']}
    for row in merged['languages.csv']:
        assert row['country'] in countries
    assert merged['languages.csv'][1]['country'] == 'DE'


def test_list_valued_foreign_key_preserves_order_and_duplicates():
    tg = make_tablegroup()
    merged = merge_tablegroup(tg, make_data(), COUNTRY_MERGE)
    langs = {row['id']: row for row in merged['languages.csv']}
    # ['DE','GER'] -> ['DE','DE']; ['GER','DE','GER'] -> ['DE','DE','DE']
    assert langs['l1']['spoken_in'] == ['DE', 'DE']
    assert langs['l2']['spoken_in'] == ['DE', 'DE', 'DE']
    assert langs['l3']['spoken_in'] == ['FR']  # untouched


def test_list_valued_fk_keeps_null_distinct_from_empty():
    tg = make_tablegroup()
    data = make_data()
    data['languages.csv'][0]['spoken_in'] = None
    merged = merge_tablegroup(tg, data, COUNTRY_MERGE)
    assert merged['languages.csv'][0]['spoken_in'] is None
    # whereas an empty list stays an empty list:
    data['languages.csv'][0]['spoken_in'] = []
    merged = merge_tablegroup(tg, data, COUNTRY_MERGE)
    assert merged['languages.csv'][0]['spoken_in'] == []


def test_list_valued_fk_concatenated_on_row_merge():
    # Two language rows merge; their list-valued FK entries concatenate.
    tg = make_tablegroup()
    data = {
        'countries.csv': [
            {'id': 'DE', 'name': 'Germany', 'capital': 'Berlin'},
            {'id': 'GER', 'name': None, 'capital': None},
        ],
        'languages.csv': [
            {'id': 'l1', 'name': 'German', 'country': 'DE',
             'spoken_in': ['DE', 'GER'], 'parent': None},
            # Non-key attributes left empty so the rows can actually combine.
            {'id': 'l2', 'name': None, 'country': None,
             'spoken_in': ['GER', 'DE', 'GER'], 'parent': None},
        ],
    }
    merged = merge_tablegroup(
        tg, data,
        {'countries.csv': {'GER': 'DE'}, 'languages.csv': {'l2': 'l1'}})
    row = next(r for r in merged['languages.csv'] if r['id'] == 'l1')
    # [DE, GER] + [GER, DE, GER], each element rewritten to canonical keys:
    assert row['spoken_in'] == ['DE', 'DE', 'DE', 'DE', 'DE']
    # Scalar attribute from the non-empty side survives:
    assert row['name'] == 'German'
    assert row['country'] == 'DE'


def test_indirect_reference_rewritten_without_owning_keymap():
    # `languages.csv` has no KeyMap of its own but references merged countries.
    tg = make_tablegroup()
    data = make_data()
    merged = merge_tablegroup(tg, data, COUNTRY_MERGE)
    # languages table untouched structurally, yet FK values are canonical:
    assert merged['languages.csv'][1]['country'] == 'DE'
    assert all(v in ('DE', 'FR')
               for row in merged['languages.csv'] for v in row['spoken_in'])


def test_self_referential_foreign_key_chain():
    tg = make_tablegroup()
    data = {
        'countries.csv': [
            {'id': 'DE', 'name': 'Germany', 'capital': 'Berlin'},
            {'id': 'GER', 'name': None, 'capital': None},
        ],
        'languages.csv': [
            {'id': 'l1', 'name': 'German', 'country': 'DE',
             'spoken_in': [], 'parent': None},
            {'id': 'l2', 'name': None, 'country': None,
             'spoken_in': [], 'parent': 'l1'},
            # chain l3 -> l2 -> l1; non-key attributes empty so rows combine.
            {'id': 'l3', 'name': None, 'country': None,
             'spoken_in': [], 'parent': 'l2'},
        ],
    }
    merged = merge_tablegroup(
        tg, data,
        {'countries.csv': {'GER': 'DE'},
         'languages.csv': {'l2': 'l1', 'l3': 'l2'}})
    remaining = {row['id']: row for row in merged['languages.csv']}
    assert set(remaining) == {'l1'}
    # l3 -> l2 -> l1 is resolved transitively on the self-referential FK.
    assert remaining['l1']['parent'] == 'l1'


def test_typed_integer_self_referential_chain_resolves():
    # Integer primary/foreign key with a mapping supplied as strings.
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'node.csv', 'tableSchema': {
            'columns': [
                {'name': 'id', 'datatype': 'integer'},
                {'name': 'parent', 'datatype': 'integer'},
                {'name': 'label'}],
            'primaryKey': ['id'],
            'foreignKeys': [{
                'columnReference': 'parent',
                'reference': {'resource': 'node.csv', 'columnReference': 'id'}}]}}]})
    data = {'node.csv': [
        {'id': 1, 'parent': None, 'label': 'root'},
        {'id': 2, 'parent': 1, 'label': None},
        {'id': 3, 'parent': 2, 'label': None},
    ]}
    merged = merge_tablegroup(
        tg, data, {'node.csv': {'2': '1', '3': '2'}})
    row = merged['node.csv'][0]
    assert row['id'] == 1
    assert row['parent'] == 1  # 3 -> 2 -> 1, parsed as integers


def test_composite_foreign_keys_rewritten_componentwise():
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'p.csv', 'tableSchema': {
            'columns': [{'name': 'a'}, {'name': 'b'}, {'name': 'note'}],
            'primaryKey': ['a', 'b']}},
        {'url': 'c.csv', 'tableSchema': {
            'columns': [{'name': 'x'}, {'name': 'y'}],
            'foreignKeys': [{
                'columnReference': ['x', 'y'],
                'reference': {'resource': 'p.csv', 'columnReference': ['a', 'b']}}]}},
    ]})
    data = {
        'p.csv': [
            {'a': '1', 'b': 'x', 'note': 'first'},
            {'a': '1', 'b': 'z', 'note': None},
        ],
        'c.csv': [{'x': '1', 'y': 'z'}],
    }
    merged = merge_tablegroup(
        tg, data, {'p.csv': {('1', 'z'): ('1', 'x')}})
    assert merged['p.csv'] == [{'a': '1', 'b': 'x', 'note': 'first'}]
    assert merged['c.csv'] == [{'x': '1', 'y': 'x'}]


def test_composite_fk_partial_null_left_untouched():
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'p.csv', 'tableSchema': {
            'columns': [{'name': 'a'}, {'name': 'b'}],
            'primaryKey': ['a', 'b']}},
        {'url': 'c.csv', 'tableSchema': {
            'columns': [{'name': 'x'}, {'name': 'y'}],
            'foreignKeys': [{
                'columnReference': ['x', 'y'],
                'reference': {'resource': 'p.csv', 'columnReference': ['a', 'b']}}]}},
    ]})
    data = {
        'p.csv': [{'a': '1', 'b': 'x'}],
        'c.csv': [{'x': None, 'y': 'x'}],
    }
    merged = merge_tablegroup(tg, data, {'p.csv': {('1', 'x'): ('2', 'x')}})
    assert merged['c.csv'] == [{'x': None, 'y': 'x'}]


# ---------------------------------------------------------------------------
# Integrity validation
# ---------------------------------------------------------------------------

def test_check_referential_integrity_detects_dangling_fk():
    tg = make_tablegroup()
    data = make_data()
    data['languages.csv'][0]['country'] = 'NOPE'
    with pytest.raises(MergeError, match='not found'):
        check_referential_integrity(tg, data)


def test_check_referential_integrity_detects_duplicate_pk():
    tg = make_tablegroup()
    data = make_data()
    data['countries.csv'].append({'id': 'DE', 'name': 'x', 'capital': 'y'})
    with pytest.raises(MergeError, match='duplicate primary key'):
        check_referential_integrity(tg, data)


def test_check_referential_integrity_list_fk_elements():
    tg = make_tablegroup()
    data = make_data()
    data['languages.csv'][0]['spoken_in'] = ['DE', 'GHOST']
    with pytest.raises(MergeError, match='not found'):
        check_referential_integrity(tg, data)


def test_check_strict_rejects_null_fk():
    tg = make_tablegroup()
    with pytest.raises(MergeError, match='null foreign key'):
        check_referential_integrity(tg, make_data(), strict=True)
    # lax (default) accepts null FKs:
    assert check_referential_integrity(tg, make_data()) is True


def test_merged_data_fails_integrity_check_on_attribute_conflict():
    # A non-key clash must abort the batch rather than silently dropping data.
    tg = make_tablegroup()
    data = make_data()
    data['countries.csv'][1]['capital'] = 'Bonn'  # clashes with 'Berlin'
    with pytest.raises(MergeError, match='conflicting values'):
        merge_tablegroup(tg, data, COUNTRY_MERGE)


# ---------------------------------------------------------------------------
# End-to-end: merged data passes the library's own checks and loads SQLite
# ---------------------------------------------------------------------------

def test_end_to_end_sqlite_load(tmp_path):
    tg = make_tablegroup()
    merged = merge_tablegroup(tg, make_data(), COUNTRY_MERGE)

    # The merged data itself satisfies primary/foreign-key constraints.
    assert check_referential_integrity(tg, merged)

    # Write merged data to CSV and validate via the TableGroup machinery.
    tg.write(tmp_path / 'md.json', **merged)
    tg2 = TableGroup.from_file(tmp_path / 'md.json')
    assert tg2.check_referential_integrity(strict=False)
    assert all(table.check_primary_key() for table in tg2.tables)

    # And it loads into SQLite, association table included.
    db = Database(tg2)
    db.write_from_tg(_force=True)
    read_back = db.read()
    assert len(read_back['countries.csv']) == 2
    lang = next(r for r in read_back['languages.csv'] if r['id'] == 'l2')
    assert lang['spoken_in'] == ['DE', 'DE', 'DE']
    with db.connection() as conn:
        conn.execute('PRAGMA foreign_keys = ON;')
        # No dangling rows in the many-to-many association table:
        n = conn.execute(
            'SELECT count(*) FROM "languages.csv_countries.csv"').fetchone()[0]
        assert n == 6
        dangling = conn.execute(
            'SELECT count(*) FROM "languages.csv_countries.csv" a '
            'LEFT JOIN "countries.csv" c ON c."id" = a."countries.csv_id" '
            'WHERE c."id" IS NULL').fetchone()[0]
        assert dangling == 0
    db._connection.close()


def test_end_to_end_composite_and_sqlite(tmp_path):
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'p.csv', 'tableSchema': {
            'columns': [{'name': 'a', 'datatype': 'integer'},
                        {'name': 'b', 'datatype': 'integer'},
                        {'name': 'note'}],
            'primaryKey': ['a', 'b']}},
        {'url': 'c.csv', 'tableSchema': {
            'columns': [
                {'name': 'id', 'datatype': 'integer'},
                {'name': 'x', 'datatype': 'integer'},
                {'name': 'y', 'datatype': 'integer'}],
            'primaryKey': ['id'],
            'foreignKeys': [{
                'columnReference': ['x', 'y'],
                'reference': {'resource': 'p.csv', 'columnReference': ['a', 'b']}}]}},
    ]})
    data = {
        'p.csv': [
            {'a': 1, 'b': 1, 'note': 'one'},
            {'a': 1, 'b': 2, 'note': None},
        ],
        'c.csv': [{'id': 7, 'x': 1, 'y': 2}],
    }
    merged = merge_tablegroup(tg, data, {'p.csv': {(1, 2): (1, 1)}})
    tg.write(tmp_path / 'md.json', **merged)
    tg2 = TableGroup.from_file(tmp_path / 'md.json')
    db = Database(tg2)
    db.write_from_tg(_force=True)
    assert db.read()['c.csv'] == [{'id': 7, 'x': 1, 'y': 1}]
    db._connection.close()


def test_keymap_objects_and_plain_dicts_equivalent():
    tg1, tg2 = make_tablegroup(), make_tablegroup()
    data1, data2 = make_data(), make_data()
    km = KeyMap('countries.csv', {'GER': 'DE'})
    a = merge_tablegroup(tg1, data1, [km])
    b = merge_tablegroup(tg2, data2, COUNTRY_MERGE)
    assert a == b


def test_input_not_mutated():
    tg = make_tablegroup()
    data = make_data()
    snapshot = copy.deepcopy(data)
    merge_tablegroup(tg, data, COUNTRY_MERGE)
    assert data == snapshot


def test_foreign_key_against_non_pk_columns_rejected_when_merging():
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'p.csv', 'tableSchema': {
            'columns': [{'name': 'id'}, {'name': 'code'}],
            'primaryKey': ['id']}},
        {'url': 'c.csv', 'tableSchema': {
            'columns': [{'name': 'ref'}],
            'foreignKeys': [{
                'columnReference': ['ref'],
                'reference': {'resource': 'p.csv', 'columnReference': ['code']}}]}},
    ]})
    data = {
        'p.csv': [{'id': '1', 'code': 'x'}, {'id': '2', 'code': 'y'}],
        'c.csv': [{'ref': 'y'}],
    }
    with pytest.raises(MergeError, match='rather than its primary key'):
        merge_tablegroup(tg, data, {'p.csv': {'2': '1'}})


def test_tables_without_keymap_pass_through_unchanged():
    tg = make_tablegroup()
    data = make_data()
    merged = merge_tablegroup(tg, data, COUNTRY_MERGE)
    # French row is not part of any mapping and stays as-is.
    fr = next(r for r in merged['countries.csv'] if r['id'] == 'FR')
    assert fr == {'id': 'FR', 'name': 'France', 'capital': 'Paris'}


def test_row_with_null_primary_key_passes_through():
    tg = make_tablegroup()
    data = make_data()
    data['countries.csv'].append({'id': None, 'name': 'Unknown', 'capital': None})
    merged = merge_tablegroup(tg, data, COUNTRY_MERGE, check=False)
    assert any(row['id'] is None for row in merged['countries.csv'])


def test_keymap_list_input(tmp_path):
    # Merging works with a list of KeyMap objects as well as a plain dict.
    tg = make_tablegroup()
    merged = merge_tablegroup(
        tg, make_data(), [KeyMap('countries.csv', {'GER': 'DE'})])
    assert check_referential_integrity(tg, merged)
    # And the result round-trips through a CSV write/read cycle.
    tg.write(tmp_path / 'md.json', **merged)
    tg2 = TableGroup.from_file(tmp_path / 'md.json')
    assert tg2.check_referential_integrity(strict=False)


def test_single_keymap_object_accepted_directly():
    tg = make_tablegroup()
    merged = merge_tablegroup(
        tg, make_data(), KeyMap('countries.csv', {'GER': 'DE'}))
    assert [r['id'] for r in merged['countries.csv']] == ['DE', 'FR']


def test_duplicate_keymap_for_same_table_rejected():
    tg = make_tablegroup()
    with pytest.raises(MergeError, match='duplicate KeyMap'):
        merge_tablegroup(tg, make_data(), [
            KeyMap('countries.csv', {'GER': 'DE'}),
            KeyMap('countries.csv', {'FR': 'DE'})])


def test_null_mapping_key_rejected():
    tg = make_tablegroup()
    with pytest.raises(MergeError, match='null is not a valid'):
        KeyMap('countries.csv', {None: 'DE'}).bind(tg.tabledict['countries.csv'])


def test_composite_keymap_rejects_non_sequence_keys():
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'names.csv', 'tableSchema': {
            'columns': [{'name': 'lang'}, {'name': 'gloss'}],
            'primaryKey': ['lang', 'gloss']}}]})
    with pytest.raises(MergeError, match='must be a sequence'):
        KeyMap('names.csv', {'a': ('a', 'b')}).bind(tg.tabledict['names.csv'])
    with pytest.raises(MergeError, match='must be a sequence'):
        KeyMap('names.csv', {('a', 'b'): 'a'}).bind(tg.tabledict['names.csv'])
    with pytest.raises(MergeError, match='has 1 components'):
        KeyMap('names.csv', {('a',): ('a', 'b')}).bind(tg.tabledict['names.csv'])


def test_foreign_key_referencing_unknown_table_rejected():
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'c.csv', 'tableSchema': {
            'columns': [{'name': 'ref'}],
            'foreignKeys': [{
                'columnReference': ['ref'],
                'reference': {'resource': 'ghost.csv', 'columnReference': ['id']}}]}}]})
    with pytest.raises(MergeError, match='references unknown table'):
        merge_tablegroup(tg, {'c.csv': [{'ref': 'x'}]}, {})


def test_schema_references_are_ignored():
    # A schemaReference (rather than a resource reference) is not an
    # inter-table relation and must be left untouched by the merge.
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'c.csv', 'tableSchema': {
            'columns': [{'name': 'id'}, {'name': 'ref'}],
            'primaryKey': ['id'],
            'foreignKeys': [{
                'columnReference': ['ref'],
                'reference': {'schemaReference': 'some-schema.json'}}]}}]})
    data = {'c.csv': [
        {'id': 'a', 'ref': 'x'}, {'id': 'b', 'ref': None}]}
    merged = merge_tablegroup(tg, data, {'c.csv': {'b': 'a'}}, check=False)
    row = next(r for r in merged['c.csv'] if r['id'] == 'a')
    assert row['ref'] == 'x'  # non-key attribute merged; schema FK ignored


def test_check_referential_integrity_rejects_null_primary_key():
    tg = make_tablegroup()
    data = make_data()
    data['countries.csv'][0]['id'] = None
    with pytest.raises(MergeError, match='null primary key'):
        check_referential_integrity(tg, data)


def test_check_strict_rejects_null_composite_foreign_key():
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'p.csv', 'tableSchema': {
            'columns': [{'name': 'a'}, {'name': 'b'}],
            'primaryKey': ['a', 'b']}},
        {'url': 'c.csv', 'tableSchema': {
            'columns': [{'name': 'x'}, {'name': 'y'}],
            'foreignKeys': [{
                'columnReference': ['x', 'y'],
                'reference': {'resource': 'p.csv', 'columnReference': ['a', 'b']}}]}},
    ]})
    data = {
        'p.csv': [{'a': '1', 'b': 'x'}],
        'c.csv': [{'x': None, 'y': 'x'}],
    }
    with pytest.raises(MergeError, match='null foreign key'):
        check_referential_integrity(tg, data, strict=True)


def test_foreign_key_into_table_without_primary_key_rejected():
    tg = TableGroup.fromvalue({'tables': [
        {'url': 'p.csv', 'tableSchema': {'columns': [{'name': 'id'}]}},
        {'url': 'c.csv', 'tableSchema': {
            'columns': [{'name': 'ref'}],
            'foreignKeys': [{
                'columnReference': ['ref'],
                'reference': {'resource': 'p.csv', 'columnReference': ['id']}}]}}]})
    data = {'p.csv': [{'id': 'x'}], 'c.csv': [{'ref': 'x'}]}
    with pytest.raises(MergeError, match='does not declare a primary key'):
        check_referential_integrity(tg, data)


def test_list_fk_concatenates_when_first_value_is_null():
    # The canonical row is encountered first with a null list FK and a later
    # duplicate carries real entries: the result adopts the later list.
    tg = make_tablegroup()
    data = {
        'countries.csv': [
            {'id': 'DE', 'name': 'Germany', 'capital': 'Berlin'},
            {'id': 'GER', 'name': None, 'capital': None},
        ],
        'languages.csv': [
            # Canonical key DE/l1 first, but its list-valued FK is null.
            {'id': 'l1', 'name': 'German', 'country': 'DE',
             'spoken_in': None, 'parent': None},
            {'id': 'l2', 'name': None, 'country': None,
             'spoken_in': ['GER', 'DE'], 'parent': None},
        ],
    }
    # The l2 row's PK is rewritten onto the already-seen canonical l1 row.
    merged = merge_tablegroup(
        tg, data,
        {'countries.csv': {'GER': 'DE'}, 'languages.csv': {'l2': 'l1'}})
    assert merged['languages.csv'][0]['spoken_in'] == ['DE', 'DE']

    # And the mirrored ordering (real list first, null duplicate) keeps it.
    data['languages.csv'][0]['spoken_in'] = ['DE']
    data['languages.csv'][1]['spoken_in'] = None
    merged = merge_tablegroup(
        tg, data,
        {'countries.csv': {'GER': 'DE'}, 'languages.csv': {'l2': 'l1'}})
    assert merged['languages.csv'][0]['spoken_in'] == ['DE']

