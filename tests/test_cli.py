import os
import json
import pathlib
import sqlite3
import argparse

import pytest

from csvw.__main__ import (
    csvw2json, csvw2datasette, csvwdescribe, csvwvalidate, csvw2markdown, csvw2sqlite)


def relpath(fname):
    return str(pathlib.Path(__file__).parent.joinpath('fixtures', fname).relative_to(os.getcwd()))


@pytest.fixture
def csvname():
    return relpath('csv.txt')


@pytest.fixture
def tsvname():
    return relpath('test.tsv')


@pytest.fixture
def mdname():
    return relpath('csv.txt-metadata.json')


@pytest.fixture
def multitable_mdname():
    return relpath('multitable/metadata.json')


def run(func, **kw):
    return func(argparse.Namespace(**kw), test=True)


def test_csvw2json(csvname, mdname, capsys):
    run(csvw2json, url=csvname)
    out, _ = capsys.readouterr()
    assert json.loads(out)
    run(csvw2json, url=mdname)
    out, _ = capsys.readouterr()
    assert json.loads(out)
    run(csvw2json, url=relpath('no-metadata.csv'))
    out, _ = capsys.readouterr()
    assert json.loads(out)


def test_csvw2markdown(mdname, multitable_mdname, capsys):
    assert run(csvw2markdown, url=mdname) == 0
    out, _ = capsys.readouterr()
    assert 'Described by' in out

    assert run(csvw2markdown, url=multitable_mdname) == 0
    out, _ = capsys.readouterr()
    assert 'References' in out


def test_csvwvalidate_2(multitable_mdname):
    assert run(csvwvalidate, url=multitable_mdname, verbose=True, lax=False) == 2
    assert run(csvwvalidate, url=multitable_mdname, verbose=True, lax=True) == 0


def test_csvwvalidate(mdname, tmp_path):
    assert run(csvwvalidate, url=mdname, lax=False) == 0
    p = tmp_path / 'md.json'
    p.write_text(pathlib.Path(mdname).read_text(encoding='utf8').replace('@context', 'context'))
    assert run(csvwvalidate, url=str(p), verbose=True, lax=False) == 2

    p.write_text(pathlib.Path(mdname).read_text(encoding='utf8').replace('"en"', '1'))
    assert run(csvwvalidate, url=str(p), verbose=True, lax=False) == 1


def test_csvwdescribe(csvname, tsvname, capsys):
    run(csvwdescribe, csv=[csvname], delimiter=',')
    out, _ = capsys.readouterr()
    assert json.loads(out)

    run(csvwdescribe, csv=[tsvname, csvname], delimiter=None)
    out, _ = capsys.readouterr()
    assert json.loads(out)


def test_csvw2datasette(tmp_path, mdname):
    run(csvw2datasette, url=mdname, outdir=tmp_path)
    assert tmp_path.joinpath('datasette.db').exists()


def test_csvw2sqlite(tmp_path, mdname):
    out = tmp_path / 'db.sqlite'
    run(csvw2sqlite, url=mdname, output=out, exact_decimals=False)
    assert out.exists()
    conn = sqlite3.connect(out)
    cu = conn.cursor()
    cu.execute('select count(*) from `csv.txt`')
    assert cu.fetchone()[0] == 2
    conn.close()


def test_csvw2sqlite_exact_decimals(tmp_path):
    md = tmp_path / 'md.json'
    csvf = tmp_path / 'data.csv'
    csvf.write_text('v\n1.2300\n-2.5\n', encoding='utf8')
    md.write_text(json.dumps({
        '@context': 'http://www.w3.org/ns/csvw',
        'tables': [{
            'url': 'data.csv',
            'tableSchema': {'columns': [{'name': 'v', 'datatype': 'decimal'}]},
        }],
    }), encoding='utf8')
    out = tmp_path / 'exact.sqlite'
    run(csvw2sqlite, url=str(md), output=out, exact_decimals=True)
    # The collation/aggregate functions are per-connection and registered by Database;
    # a bare sqlite3 connection still sees the exact TEXT storage (never REAL).
    conn = sqlite3.connect(out)
    stored = conn.execute('SELECT typeof(v), v FROM `data.csv`').fetchall()
    conn.close()
    assert sorted(stored) == [('text', '-2.5'), ('text', '1.2300')]
