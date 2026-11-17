csvw.merge
==========

.. automodule:: csvw.merge
    :members:
    :undoc-members:

Merging rows that describe the same entity
------------------------------------------

After cleaning data it frequently turns out that several business keys
denote the same real-world entity. Merging the corresponding rows properly
means rewriting the actual data - not just the metadata: the rows of the
referenced table as well as every foreign key pointing at the merged keys
must be updated.

A merge is driven by an explicit mapping of old keys to a canonical key per
table, represented by :class:`~csvw.merge.KeyMap`. Mappings may form chains
which are resolved transitively; cycles are rejected with
:class:`~csvw.merge.MergeError`. Keys are parsed according to the datatype
declared for the corresponding primary key column(s), so scalar and
composite primary keys are both supported.

.. code-block:: python

    from csvw import TableGroup, KeyMap, merge_tablegroup

    tg = TableGroup.from_file('metadata.json')
    data = tg.read()

    merged = merge_tablegroup(tg, data, {
        # old business key -> canonical key; chains are allowed, cycles are not:
        'countries.csv': {'GER': 'DE', 'DE': 'DEU'},
        # integer keys supplied as strings are parsed using the column datatype:
        'languages.csv': {'2': '1'},
        # composite keys are tuples in primary-key order:
        'names.csv': {('a', 'x'): ('a', 'y')},
    })

The function rewrites **all** declared foreign keys - scalar, composite and
list-valued (many-to-many) ones - including foreign keys on tables that are
not themselves merged but reference a merged table. Non-key attributes of
merged rows are only combined when equal or one-sided empty; any other clash
aborts the whole batch. Entries of list-valued foreign-key columns are
concatenated in row order, preserving order and duplicates.

The returned data passes primary/foreign-key validation
(:func:`~csvw.merge.check_referential_integrity`) and can be written back to
CSV or loaded into SQLite via :class:`csvw.db.Database`.
