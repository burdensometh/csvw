"""
Tests for multi-source master data merging (`csvw.merge`).
"""
import json
import pathlib
import sqlite3

import pytest

from csvw import Table, TableGroup
from csvw.merge import Source, MasterMerger, MergeConflict, merge_sources


def make_table() -> Table:
    """A table with a business key `id` and typed, mergeable value columns."""
    return Table.fromvalue({
        'url': 'entities.csv',
        'tableSchema': {
            'primaryKey': ['id'],
            'columns': [
                {'name': 'id'},
                {'name': 'name'},
                {'name': 'age', 'datatype': 'integer'},
                {'name': 'score', 'datatype': 'decimal'},
                {'name': 'tags', 'datatype': 'string', 'separator': ';'},
            ],
        },
    })


def write_csv(path: pathlib.Path, header, *rows):
    path.write_text(
        ','.join(header) + '\n' + '\n'.join(','.join(r) for r in rows) + '\n',
        encoding='utf8')


@pytest.fixture
def table():
    return make_table()


def test_basic_merge_priority_and_provenance(tmp_path, table):
    crm = Source('crm', priorities={'name': 1, 'age': 2})
    erp = Source('erp', priorities={'name': 2, 'age': 1, 'score': 1})
    merger = MasterMerger(table, tmp_path / 'out', [crm, erp], round_id='r1')

    result = merger.run(rows={
        'crm': [
            {'id': '1', 'name': 'Acme', 'age': '10', 'score': None, 'tags': None},
            {'id': '2', 'name': 'Beta', 'age': None, 'score': None, 'tags': None},
        ],
        'erp': [
            {'id': '1', 'name': 'Acme Inc.', 'age': '12', 'score': '3.5', 'tags': None},
        ],
    })

    master = {r['id']: r for r in result.master}
    # Lower priority number wins per field.
    assert master['1']['name'] == 'Acme'
    assert master['1']['age'] == 12
    assert master['1']['score'] == pytest.approx(3.5)
    # Entity 2 only exists in the crm source.
    assert master['2']['name'] == 'Beta'
    assert master['2']['age'] is None

    prov = {(p.key[0], p.field): p for p in result.provenance}
    assert prov[('1', 'name')].source == 'crm'
    assert prov[('1', 'age')].source == 'erp'
    assert prov[('1', 'score')].source == 'erp'
    # No provenance for fields with no contribution at all.
    assert ('1', 'tags') not in prov
    assert all(p.round_id == 'r1' for p in result.provenance)


def test_plain_null_does_not_override(tmp_path, table):
    crm = Source('crm', priorities={'name': 1})  # null_overrides defaults to False
    erp = Source('erp', priorities={'name': 2})
    merger = MasterMerger(table, tmp_path / 'out', [crm, erp], round_id='r1')

    result = merger.run(rows={
        'crm': [{'id': '1', 'name': None}],
        'erp': [{'id': '1', 'name': 'Acme'}],
    })
    assert result.master[0]['name'] == 'Acme'
    # The winning contribution is still attributed to the source that supplied it.
    assert result.provenance[0].source == 'erp'


def test_null_overrides_restores_and_clears(tmp_path, table):
    crm = Source('crm', priorities={'name': 1}, null_overrides=True)
    erp = Source('erp', priorities={'name': 2})
    merger = MasterMerger(table, tmp_path / 'out', [crm, erp], round_id='r1')

    result = merger.run(rows={
        'crm': [{'id': '1', 'name': None}],
        'erp': [{'id': '1', 'name': 'Acme'}],
    })
    assert result.master[0]['name'] is None
    # With null override, the null contribution itself is the winner.
    assert result.provenance[0].source == 'crm'

    # Per-field opt-in works as well.
    other = MasterMerger(
        table, tmp_path / 'out2',
        [Source('a', priorities={'name': 1, 'age': 1}, null_overrides=['name']),
         Source('b', priorities={'name': 2, 'age': 2})],
        round_id='r1')
    result = other.run(rows={
        'a': [{'id': '1', 'name': None, 'age': None}],
        'b': [{'id': '1', 'name': 'Acme', 'age': 30}],
    })
    row = result.master[0]
    assert row['name'] is None       # override enabled for name
    assert row['age'] == 30          # override disabled for age


def test_same_priority_equal_values_are_fine(tmp_path, table):
    merger = MasterMerger(
        table, tmp_path / 'out',
        [Source('a', default_priority=1), Source('b', default_priority=1)],
        round_id='r1')
    result = merger.run(rows={
        'a': [{'id': '1', 'name': 'Acme', 'age': 5}],
        'b': [{'id': '1', 'name': 'Acme', 'age': 5}],
    })
    assert result.master[0]['name'] == 'Acme'
    assert result.master[0]['age'] == 5


def test_same_priority_conflicting_values_raise(tmp_path, table):
    merger = MasterMerger(
        table, tmp_path / 'out',
        [Source('a', default_priority=1), Source('b', default_priority=1)],
        round_id='r1')
    with pytest.raises(MergeConflict) as exc:
        merger.run(rows={
            'a': [{'id': '1', 'name': 'Acme', 'age': 5}],
            'b': [{'id': '1', 'name': 'Acmex', 'age': 6}],
        })
    fields = {c.field for c in exc.value.conflicts}
    assert fields == {'name', 'age'}
    detail = next(c for c in exc.value.conflicts if c.field == 'name')
    assert detail.key == ('1',)
    assert detail.values == {'a': 'Acme', 'b': 'Acmex'}
    assert detail.priority == 1
    # Nothing was published.
    assert not (tmp_path / 'out').exists() or not (tmp_path / 'out' / 'master.csv').exists()


def test_failed_run_leaves_previous_round_intact(tmp_path, table):
    merger = MasterMerger(
        table, tmp_path / 'out',
        [Source('a', default_priority=1), Source('b', default_priority=1)],
        round_id='r1')
    merger.run(rows={
        'a': [{'id': '1', 'name': 'Acme'}],
        'b': [{'id': '1', 'name': 'Acme'}],
    })
    first_master = (tmp_path / 'out' / 'master.csv').read_text(encoding='utf8')
    first_db = (tmp_path / 'out' / 'master.sqlite').read_bytes()

    merger.round_id = 'r2'
    with pytest.raises(MergeConflict):
        merger.run(rows={
            'a': [{'id': '1', 'name': 'Acme'}],
            'b': [{'id': '1', 'name': 'BOOM'}],
        })

    # All outputs still describe round r1; no staging directory is left behind.
    assert (tmp_path / 'out' / 'master.csv').read_text(encoding='utf8') == first_master
    assert (tmp_path / 'out' / 'master.sqlite').read_bytes() == first_db
    state = json.loads((tmp_path / 'out' / 'state.json').read_text(encoding='utf8'))
    assert state['round'] == 'r1'
    assert not list((tmp_path / 'out').glob('csvw-merge-*'))


def test_snapshot_deletion_restores_other_source_value(tmp_path, table):
    merger = MasterMerger(
        table, tmp_path / 'out',
        [Source('crm', priorities={'name': 1, 'age': 1}),
         Source('erp', priorities={'name': 2, 'age': 2})],
        round_id='r1')

    merger.run(rows={
        'crm': [{'id': '1', 'name': 'Acme', 'age': 10}],
        'erp': [{'id': '1', 'name': 'Acme ERP', 'age': 12}],
    })
    assert merger.current_master()[0]['name'] == 'Acme'

    # Round 2: the crm withdraws the `name` field and deletes its age value entirely.
    merger.round_id = 'r2'
    result = merger.run(rows={
        'crm': [{'id': '1'}],
        'erp': [{'id': '1', 'name': 'Acme ERP', 'age': 12}],
    })
    row = result.master[0]
    assert row['name'] == 'Acme ERP'   # restored from the other still-valid source
    assert row['age'] == 12
    prov = {(p.key[0], p.field): p for p in result.provenance}
    assert prov[('1', 'name')].source == 'erp'
    assert prov[('1', 'name')].round_id == 'r2'

    # Round 3: the crm deletes the whole record; erp data still keeps the entity alive.
    merger.round_id = 'r3'
    result = merger.run(rows={'crm': [], 'erp': [
        {'id': '1', 'name': 'Acme ERP', 'age': 12}]})
    assert [r['id'] for r in result.master] == ['1']
    assert result.master[0]['name'] == 'Acme ERP'

    # Round 4: the last source deletes the record too - the entity disappears.
    merger.round_id = 'r4'
    result = merger.run(rows={'crm': [], 'erp': []})
    assert result.master == []


def test_snapshots_are_persisted_and_typed(tmp_path, table):
    merger = MasterMerger(
        table, tmp_path / 'out',
        [Source('crm', path=tmp_path / 'crm.csv',
                priorities={'name': 1, 'age': 1, 'score': 1, 'tags': 1})],
        round_id='r1')
    write_csv(tmp_path / 'crm.csv',
              ['id', 'name', 'age', 'score', 'tags'],
              ['1', 'Acme', '7', '2.5', 'a;b'])
    merger.run()

    snapshot = json.loads(
        (tmp_path / 'out' / 'snapshots' / 'crm.json').read_text(encoding='utf8'))
    assert snapshot['rows'] == [
        {'id': '1', 'name': 'Acme', 'age': '7', 'score': '2.5', 'tags': 'a;b'}]

    # A later run without fresh data rebuilds from the persisted snapshot.
    merger2 = MasterMerger.from_state(tmp_path / 'out', round_id='r2')
    result = merger2.run()
    row = result.master[0]
    assert row['age'] == 7                       # typed via the column datatype
    assert row['score'] == pytest.approx(2.5)
    assert row['tags'] == ['a', 'b']             # list-valued column


def test_published_outputs_share_one_round(tmp_path, table):
    merger = MasterMerger(
        table, tmp_path / 'out',
        [Source('crm', priorities={'name': 1}),
         Source('erp', priorities={'name': 2, 'age': 1, 'score': 1})],
        round_id='r1')
    result = merger.run(rows={
        'crm': [{'id': '1', 'name': 'Acme'}, {'id': '2', 'name': 'Beta'}],
        'erp': [{'id': '1', 'name': 'Acme Inc.', 'age': 9, 'score': '1.5'}],
    })

    # The typed master table on disk round-trips through the CSVW schema.
    on_disk = merger.current_master()
    assert [r['id'] for r in on_disk] == ['1', '2']
    assert on_disk[0]['age'] == 9
    assert on_disk[0]['score'] == pytest.approx(1.5)

    # Provenance CSV and the round column.
    tg = TableGroup.from_file(tmp_path / 'out' / 'master-metadata.json')
    prov = list(tg.tabledict['field-sources.csv'].iterdicts(
        fname=tmp_path / 'out' / 'field-sources.csv'))
    assert {r['round'] for r in prov} == {'r1'}
    assert prov[0]['priority'] >= 1
    assert all(r['source'] in ('crm', 'erp') for r in prov)

    # SQLite contains the same rows, keyed identically.
    rows = merger.query('SELECT `id`, `name`, `age` FROM `master.csv` ORDER BY `id`')
    assert rows == [('1', 'Acme', 9), ('2', 'Beta', None)]
    db_rounds = merger.query('SELECT DISTINCT `round` FROM `field-sources.csv`')
    assert db_rounds == [('r1',)]
    with sqlite3.connect(str(tmp_path / 'out' / 'master.sqlite')) as conn:
        assert conn.execute(
            'SELECT count(*) FROM `master.csv`').fetchone()[0] == len(result.master)
        assert conn.execute(
            'SELECT count(*) FROM `field-sources.csv`').fetchone()[0] \
            == len(result.provenance)


def test_read_sources_from_csv_files(tmp_path, table):
    write_csv(tmp_path / 'crm.csv', ['id', 'name', 'age'],
              ['1', 'Acme', '10'], ['2', 'Beta', ''])
    write_csv(tmp_path / 'erp.csv', ['id', 'name', 'age'], ['1', 'Acmex', '12'])
    merger = MasterMerger(
        table, tmp_path / 'out',
        [Source('crm', path=tmp_path / 'crm.csv', priorities={'name': 1, 'age': 2}),
         Source('erp', path=tmp_path / 'erp.csv', priorities={'name': 2, 'age': 1})],
        round_id='r1')
    result = merger.run()
    master = {r['id']: r for r in result.master}
    assert master['1']['name'] == 'Acme'
    assert master['1']['age'] == 12
    assert master['2']['name'] == 'Beta'


def test_missing_business_key_is_an_error(tmp_path, table):
    merger = MasterMerger(
        table, tmp_path / 'out', [Source('crm', default_priority=1)], round_id='r1')
    with pytest.raises(ValueError, match='business key'):
        merger.run(rows={'crm': [{'id': None, 'name': 'Acme'}]})


def test_unknown_field_in_config_is_an_error(tmp_path, table):
    with pytest.raises(ValueError, match='unknown fields'):
        MasterMerger(table, tmp_path / 'out',
                     [Source('crm', priorities={'nope': 1})], round_id='r1')
    with pytest.raises(ValueError, match='unknown fields'):
        MasterMerger(table, tmp_path / 'out',
                     [Source('crm', null_overrides=['nope'])], round_id='r1')


def test_composite_business_key(tmp_path):
    table = Table.fromvalue({
        'url': 'links.csv',
        'tableSchema': {
            'primaryKey': ['src', 'dst'],
            'columns': [
                {'name': 'src'}, {'name': 'dst'}, {'name': 'weight', 'datatype': 'integer'}],
        },
    })
    merger = MasterMerger(
        table, tmp_path / 'out',
        [Source('a', default_priority=1), Source('b', default_priority=2)], round_id='r1')
    result = merger.run(rows={
        'a': [{'src': 'x', 'dst': 'y', 'weight': 1}],
        'b': [{'src': 'x', 'dst': 'y', 'weight': 2},
              {'src': 'y', 'dst': 'z', 'weight': 3}],
    })
    assert result.keys == [('x', 'y'), ('y', 'z')]
    assert result.master[0]['weight'] == 1
    rows = merger.query('SELECT `src`, `dst`, `weight` FROM `master.csv` ORDER BY `src`')
    assert rows == [('x', 'y', 1), ('y', 'z', 3)]


def test_list_values_conflict_and_equality(tmp_path, table):
    merger = MasterMerger(
        table, tmp_path / 'out',
        [Source('a', default_priority=1), Source('b', default_priority=1)], round_id='r1')
    # Equal lists at the same priority: no conflict.
    result = merger.run(rows={
        'a': [{'id': '1', 'tags': ['x', 'y']}],
        'b': [{'id': '1', 'tags': ['x', 'y']}],
    })
    assert result.master[0]['tags'] == ['x', 'y']

    with pytest.raises(MergeConflict) as exc:
        merger.run(rows={
            'a': [{'id': '1', 'tags': ['x', 'y']}],
            'b': [{'id': '1', 'tags': ['x', 'z']}],
        })
    assert exc.value.conflicts[0].field == 'tags'


def test_convenience_wrapper(tmp_path, table):
    result = merge_sources(
        table, tmp_path / 'out',
        [Source('crm', priorities={'name': 1}), Source('erp', priorities={'name': 2})],
        rows={'crm': [{'id': '1', 'name': 'Acme'}],
              'erp': [{'id': '1', 'name': 'Acme ERP'}]},
        round_id='r1')
    assert result.round_id == 'r1'
    assert result.master[0]['name'] == 'Acme'
    assert (tmp_path / 'out' / 'master.sqlite').exists()


def test_duplicate_business_key_within_source(tmp_path, table):
    merger = MasterMerger(
        table, tmp_path / 'out', [Source('crm', default_priority=1)], round_id='r1')
    with pytest.raises(ValueError, match='duplicate business key'):
        merger.run(rows={'crm': [
            {'id': '1', 'name': 'Acme'}, {'id': '1', 'name': 'Acme 2'}]})
    assert not (tmp_path / 'out').exists() or \
        not (tmp_path / 'out' / 'master.csv').exists()


def test_failure_during_publish_rolls_back(tmp_path, table, monkeypatch):
    merger = MasterMerger(
        table, tmp_path / 'out',
        [Source('crm', default_priority=1)], round_id='r1')
    merger.run(rows={'crm': [{'id': '1', 'name': 'Acme'}]})
    first = (tmp_path / 'out' / 'master.csv').read_text(encoding='utf8')

    # Break the SQLite load of the second round after master/provenance were staged.
    def boom(*_args, **_kwargs):
        raise RuntimeError('disk full')
    monkeypatch.setattr('csvw.merge.Database.write', boom)
    merger.round_id = 'r2'
    with pytest.raises(RuntimeError, match='disk full'):
        merger.run(rows={'crm': [{'id': '1', 'name': 'Acme 2'}]})

    assert (tmp_path / 'out' / 'master.csv').read_text(encoding='utf8') == first
    assert json.loads((tmp_path / 'out' / 'state.json').read_text())['round'] == 'r1'
    assert not list((tmp_path / 'out').glob('csvw-merge-*'))


def test_empty_sources_produce_empty_master(tmp_path, table):
    merger = MasterMerger(
        table, tmp_path / 'out', [Source('crm', default_priority=1)], round_id='r1')
    result = merger.run(rows={'crm': []})
    assert result.master == []
    assert result.provenance == []
    on_disk = merger.current_master()
    assert on_disk == []
    assert merger.query('SELECT count(*) FROM `master.csv`') == [(0,)]


def test_zero_and_false_are_valid_non_null_values(tmp_path, table):
    bool_table = Table.fromvalue({
        'url': 'flags.csv',
        'tableSchema': {'primaryKey': ['id'], 'columns': [
            {'name': 'id'},
            {'name': 'age', 'datatype': 'integer'},
            {'name': 'active', 'datatype': 'boolean'},
        ]}})
    merger = MasterMerger(
        bool_table, tmp_path / 'out',
        [Source('a', default_priority=1), Source('b', default_priority=2)], round_id='r1')
    result = merger.run(rows={
        'a': [{'id': '1', 'age': 0, 'active': False}],
        'b': [{'id': '1', 'age': None, 'active': None}],
    })
    row = result.master[0]
    assert row['age'] == 0
    assert row['active'] is False


def test_snapshot_rebuilds_when_source_is_not_refreshed(tmp_path, table):
    merger = MasterMerger(
        table, tmp_path / 'out',
        [Source('crm', priorities={'name': 1, 'age': 1}),
         Source('erp', priorities={'name': 2, 'age': 2})], round_id='r1')
    merger.run(rows={
        'crm': [{'id': '1', 'name': 'Acme', 'age': 10}],
        'erp': [{'id': '1', 'name': 'Acme ERP', 'age': 12}],
    })

    # Round 2 refreshes only crm (inline); erp is rebuilt from its persisted snapshot.
    merger.round_id = 'r2'
    result = merger.run(rows={
        'crm': [{'id': '1', 'name': 'Acme GmbH', 'age': 11}],
    })
    row = result.master[0]
    assert row['name'] == 'Acme GmbH'
    assert row['age'] == 11
    erp_snapshot = json.loads(
        (tmp_path / 'out' / 'snapshots' / 'erp.json').read_text(encoding='utf8'))
    assert erp_snapshot['rows'][0]['name'] == 'Acme ERP'


def test_state_records_configuration(tmp_path, table):
    sources = [Source('crm', path='crm.csv', priorities={'name': 1}, null_overrides=['name']),
               Source('erp', default_priority=5)]
    MasterMerger(table, tmp_path / 'out', sources, round_id='r1').run(rows={
        'crm': [{'id': '1', 'name': 'Acme'}],
        'erp': [{'id': '1', 'name': 'Acme ERP'}],
    })
    state = json.loads((tmp_path / 'out' / 'state.json').read_text(encoding='utf8'))
    assert state['business_key'] == ['id']
    assert state['table'] == 'entities.csv'
    restored = MasterMerger.from_state(tmp_path / 'out')
    assert [s.name for s in restored.sources] == ['crm', 'erp']
    assert restored.sources[0].null_overrides_for('name') is True
    assert restored.sources[0].null_overrides_for('age') is False
    assert restored.sources[1].priority_for('name') == 5
