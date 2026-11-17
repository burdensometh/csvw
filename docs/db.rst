`csvw.db`
=========

.. automodule:: csvw.db
   :members:

Exact decimal mode
------------------

By default, ``xs:decimal`` columns are stored as SQLite ``REAL`` values, i.e. binary
floating point numbers. Values such as monetary amounts can therefore lose low-order
digits on the round-trip through the database.

Passing ``exact_decimals=True`` when creating a :class:`~csvw.db.Database` switches
``xs:decimal`` columns to an exact representation:

- finite decimal values are stored verbatim as canonical, fixed-point decimal text
  (SQLite ``TEXT``), never as floats;
- a dedicated numeric collation (``csvw_decimal``) makes plain SQL comparisons,
  ``ORDER BY``, ``GROUP BY``, ``DISTINCT``, ``min()`` and ``max()`` operate on the
  *numeric* value rather than the lexicographic order of the text;
- the ``csvw_decimal_sum`` aggregate sums values without binary floating point and
  without rounding to the precision of the default :class:`decimal.Decimal` context;
- ``NULL``, negative numbers and values with different numbers of decimal places are
  all supported.

Ordinary (non-decimal) columns are unaffected, and databases created with the default
``exact_decimals=False`` keep their previous ``REAL`` storage and behaviour.

.. code-block:: python

    from decimal import Decimal
    from csvw.db import Database

    db = Database(tablegroup, exact_decimals=True)
    db.write_from_tg()

    # Numeric ordering: '-2.5' < '9.99' < '100' (not lexicographic text ordering).
    db.query("SELECT amount FROM ledger ORDER BY amount")

    # Exact, context-independent summation.
    db.query("SELECT party, csvw_decimal_sum(amount) FROM ledger GROUP BY party")

:class:`decimal.Decimal` query parameters are adapted to the same canonical text
representation automatically. Floats cannot be written to exact-decimal columns; use
``Decimal``, ``int`` or ``str`` values instead.
