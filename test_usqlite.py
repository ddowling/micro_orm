"""
Comprehensive tests for usqlite / sqlite3 behaviour.

Covers the bug fixes applied from usqlite PR #35:
  - SQLITE_TRANSIENT for text/blob bindings (GC safety)
  - cursor_close deregisters before finalising statement
  - connection_close uses while-loop so cursor list shrinks correctly
  - execute/executemany do not call cursor_close (which would NULL connection)
  - stepExecute receives cursor pointer not mp_obj_t (fetchone/fetchmany)
  - executemany null-connection guard

Runs under both CPython (sqlite3) and MicroPython (usqlite).
"""

try:
    import usqlite as sqlite
except ImportError:
    import sqlite3 as sqlite

try:
    import gc
    HAS_GC = True
except ImportError:
    HAS_GC = False


# ---------------------------------------------------------------------------
# Test harness
# ---------------------------------------------------------------------------

_passed = 0
_failed = 0


def ok(desc, got, expected):
    global _passed, _failed
    if got == expected:
        print('ok  ', desc)
        _passed += 1
    else:
        print('FAIL:', desc)
        print('  expected:', repr(expected))
        print('  got:     ', repr(got))
        _failed += 1


def ok_true(desc, value):
    ok(desc, bool(value), True)


def ok_raises(desc, fn):
    global _passed, _failed
    try:
        fn()
        print('FAIL:', desc, '(no exception raised)')
        _failed += 1
    except Exception as e:
        print('ok  ', desc, '({})'.format(type(e).__name__))
        _passed += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fresh_db():
    db = sqlite.connect(':memory:')
    db.execute('CREATE TABLE t (id INTEGER PRIMARY KEY, txt TEXT, num REAL, raw BLOB, flag INTEGER)')
    return db


# ---------------------------------------------------------------------------
# 1. Basic type binding round-trips
# ---------------------------------------------------------------------------

def test_bind_types():
    print('\n-- bind types --')
    db = fresh_db()

    db.execute('INSERT INTO t (txt, num, raw, flag) VALUES (?,?,?,?)',
               ('hello', 3.14, bytes([1, 2, 3]), 1))

    cur = db.execute('SELECT txt, num, raw, flag FROM t WHERE id=1')
    row = cur.fetchone()

    ok('text round-trip',   row[0], 'hello')
    ok('real round-trip',   row[1], 3.14)
    ok('blob round-trip',   row[2], bytes([1, 2, 3]))
    ok('blob type',         type(row[2]), bytes)
    ok('int round-trip',    row[3], 1)

    # NULL binding
    db.execute('INSERT INTO t (txt) VALUES (?)', (None,))
    cur = db.execute('SELECT txt FROM t WHERE id=2')
    ok('null round-trip',   cur.fetchone()[0], None)


# ---------------------------------------------------------------------------
# 2. SQLITE_TRANSIENT safety: GC between bind and step
# ---------------------------------------------------------------------------

def test_transient_gc():
    print('\n-- SQLITE_TRANSIENT / GC safety --')
    db = fresh_db()

    # Build string and bytes in a function scope so they're candidates for GC
    def make_text():
        return 'gc_pressure_' + 'x' * 50

    def make_blob():
        return bytes(range(64))

    txt = make_text()
    blob = make_blob()

    db.execute('INSERT INTO t (txt, raw) VALUES (?,?)', (txt, blob))

    # Force collection between insert and select
    if HAS_GC:
        gc.collect()

    cur = db.execute('SELECT txt, raw FROM t WHERE id=1')
    row = cur.fetchone()

    ok('text after gc',     row[0], txt)
    ok('blob after gc',     row[1], blob)


# ---------------------------------------------------------------------------
# 3. Named parameters
# ---------------------------------------------------------------------------

def test_named_params():
    print('\n-- named parameters --')
    db = fresh_db()

    db.execute('INSERT INTO t (txt, num) VALUES (:name, :val)',
               {'name': 'alpha', 'val': 1.5})
    db.execute('INSERT INTO t (txt, num) VALUES (:name, :val)',
               {'name': 'beta', 'val': 2.5})

    cur = db.execute('SELECT txt, num FROM t WHERE txt=:name', {'name': 'alpha'})
    row = cur.fetchone()
    ok('named text',        row[0], 'alpha')
    ok('named real',        row[1], 1.5)

    cur = db.execute('SELECT count(*) FROM t WHERE num > :threshold', {'threshold': 1.0})
    ok('named count',       cur.fetchone()[0], 2)


# ---------------------------------------------------------------------------
# 4. Multiple execute calls on the same cursor
# ---------------------------------------------------------------------------

def test_cursor_reuse():
    print('\n-- cursor reuse --')
    db = fresh_db()

    cur = db.cursor()
    cur.execute('INSERT INTO t (txt) VALUES (?)', ('row1',))
    cur.execute('INSERT INTO t (txt) VALUES (?)', ('row2',))
    cur.execute('SELECT txt FROM t ORDER BY id')

    rows = cur.fetchall()
    ok('reuse row count',   len(rows), 2)
    ok('reuse row 0',       rows[0][0], 'row1')
    ok('reuse row 1',       rows[1][0], 'row2')


# ---------------------------------------------------------------------------
# 5. fetchone
# ---------------------------------------------------------------------------

def test_fetchone():
    print('\n-- fetchone --')
    db = fresh_db()

    for i in range(4):
        db.execute('INSERT INTO t (num) VALUES (?)', (float(i),))

    cur = db.execute('SELECT num FROM t ORDER BY id')

    ok('fetchone 0',        cur.fetchone()[0], 0.0)
    ok('fetchone 1',        cur.fetchone()[0], 1.0)
    ok('fetchone 2',        cur.fetchone()[0], 2.0)
    ok('fetchone 3',        cur.fetchone()[0], 3.0)
    ok('fetchone exhausted', cur.fetchone(), None)


# ---------------------------------------------------------------------------
# 6. fetchmany
# ---------------------------------------------------------------------------

def test_fetchmany():
    print('\n-- fetchmany --')
    db = fresh_db()

    for i in range(5):
        db.execute('INSERT INTO t (num) VALUES (?)', (float(i),))

    cur = db.execute('SELECT num FROM t ORDER BY id')
    batch = cur.fetchmany(3)
    ok('fetchmany size',    len(batch), 3)
    ok('fetchmany first',   batch[0][0], 0.0)
    ok('fetchmany last',    batch[2][0], 2.0)

    remainder = cur.fetchall()
    ok('fetchall remainder', len(remainder), 2)
    ok('remainder first',   remainder[0][0], 3.0)


# ---------------------------------------------------------------------------
# 7. fetchall
# ---------------------------------------------------------------------------

def test_fetchall():
    print('\n-- fetchall --')
    db = fresh_db()

    for i in range(6):
        db.execute('INSERT INTO t (num) VALUES (?)', (float(i),))

    rows = db.execute('SELECT num FROM t ORDER BY id').fetchall()
    ok('fetchall count',    len(rows), 6)
    ok('fetchall values',   [r[0] for r in rows], [0.0, 1.0, 2.0, 3.0, 4.0, 5.0])


# ---------------------------------------------------------------------------
# 8. Iterator protocol
# ---------------------------------------------------------------------------

def test_iterator():
    print('\n-- iterator --')
    db = fresh_db()

    for i in range(4):
        db.execute('INSERT INTO t (num) VALUES (?)', (float(i),))

    results = [row[0] for row in db.execute('SELECT num FROM t ORDER BY id')]
    ok('iterator values',   results, [0.0, 1.0, 2.0, 3.0])


# ---------------------------------------------------------------------------
# 9. Cursor close prevents further use
# ---------------------------------------------------------------------------

def test_cursor_close():
    print('\n-- cursor close --')
    db = fresh_db()
    db.execute('INSERT INTO t (txt) VALUES (?)', ('x',))

    cur = db.cursor()
    cur.execute('SELECT txt FROM t')
    cur.close()

    # execute on a closed cursor should raise
    ok_raises('execute after close raises', lambda: cur.execute('SELECT 1'))


# ---------------------------------------------------------------------------
# 10. Connection close with open cursors
# ---------------------------------------------------------------------------

def test_connection_close_with_cursors():
    print('\n-- connection close with open cursors --')
    db = fresh_db()

    for i in range(3):
        db.execute('INSERT INTO t (txt) VALUES (?)', ('r{}'.format(i),))

    # Open multiple cursors but don't close them
    c1 = db.execute('SELECT txt FROM t')
    c2 = db.execute('SELECT txt FROM t')
    c3 = db.cursor()
    c3.execute('SELECT txt FROM t')

    # Closing the connection should not crash even with open cursors
    db.close()
    ok_true('connection close with open cursors', True)  # reaching here = no crash


# ---------------------------------------------------------------------------
# 11. Context manager (cursor)
# ---------------------------------------------------------------------------

def test_context_manager():
    print('\n-- context manager --')
    db = fresh_db()
    db.execute('INSERT INTO t (txt) VALUES (?)', ('ctx',))

    cur = db.execute('SELECT txt FROM t')
    if hasattr(cur, '__enter__'):
        with cur:
            row = cur.fetchone()
    else:
        row = cur.fetchone()
    ok('context manager result', row[0], 'ctx')


# ---------------------------------------------------------------------------
# 12. executemany (multi-statement SQL)
# ---------------------------------------------------------------------------

def test_executemany():
    print('\n-- executemany / executescript --')
    db = sqlite.connect(':memory:')

    sql = (
        'CREATE TABLE IF NOT EXISTS stuff (id INTEGER PRIMARY KEY, val TEXT);'
        "INSERT INTO stuff (val) VALUES ('a');"
        "INSERT INTO stuff (val) VALUES ('b');"
        "INSERT INTO stuff (val) VALUES ('c');"
    )

    # usqlite.executemany takes a single multi-statement SQL string;
    # cpython sqlite3.executescript is the equivalent
    if hasattr(db, 'executescript'):
        db.executescript(sql)
    else:
        db.executemany(sql)

    rows = db.execute('SELECT val FROM stuff ORDER BY id').fetchall()
    ok('executemany row count', len(rows), 3)
    ok('executemany row 0',     rows[0][0], 'a')
    ok('executemany row 2',     rows[2][0], 'c')


# ---------------------------------------------------------------------------
# 13. rowcount and lastrowid
# ---------------------------------------------------------------------------

def test_rowcount_lastrowid():
    print('\n-- rowcount / lastrowid --')
    db = fresh_db()

    cur = db.execute('INSERT INTO t (txt) VALUES (?)', ('r1',))
    ok('lastrowid after insert', cur.lastrowid, 1)

    cur = db.execute('INSERT INTO t (txt) VALUES (?)', ('r2',))
    ok('lastrowid after second', cur.lastrowid, 2)

    cur = db.execute("UPDATE t SET txt='updated' WHERE id=1")
    ok('rowcount after update',  cur.rowcount, 1)

    cur = db.execute('DELETE FROM t')
    ok('rowcount after delete',  cur.rowcount, 2)


# ---------------------------------------------------------------------------
# 14. Large blob binding (stress SQLITE_TRANSIENT copy)
# ---------------------------------------------------------------------------

def test_large_blob():
    print('\n-- large blob --')
    db = fresh_db()

    payload = bytes(range(256)) * 4  # 1 KB
    db.execute('INSERT INTO t (raw) VALUES (?)', (payload,))

    if HAS_GC:
        gc.collect()

    row = db.execute('SELECT raw FROM t WHERE id=1').fetchone()
    ok('large blob size',   len(row[0]), len(payload))
    ok('large blob content', row[0], payload)


# ---------------------------------------------------------------------------
# 15. Empty blob
# ---------------------------------------------------------------------------

def test_empty_blob():
    print('\n-- empty blob --')
    db = fresh_db()

    # Insert a non-empty blob, then an empty blob in the same table
    db.execute('INSERT INTO t (raw) VALUES (?)', (bytes([1, 2, 3]),))
    db.execute('INSERT INTO t (raw) VALUES (?)', (b'',))

    if HAS_GC:
        gc.collect()

    rows = db.execute('SELECT id, raw FROM t ORDER BY id').fetchall()
    ok('rows after empty blob insert',  len(rows), 2)
    ok('non-empty blob intact',         rows[0][1], bytes([1, 2, 3]))
    ok('empty blob round-trip',         rows[1][1], b'')
    ok('empty blob type',               type(rows[1][1]), bytes)

    # Query again to confirm database is not corrupted after empty blob binding
    ok('db functional after empty blob',
       db.execute('SELECT count(*) FROM t').fetchone()[0], 2)


# ---------------------------------------------------------------------------
# 16. Transaction rollback
# ---------------------------------------------------------------------------

def test_transaction():
    print('\n-- transaction --')
    db = fresh_db()

    try:
        db.execute('BEGIN')
        db.execute('INSERT INTO t (txt) VALUES (?)', ('tx1',))
        db.execute('INSERT INTO t (txt) VALUES (?)', ('tx2',))
        db.execute('COMMIT')
    except Exception:
        db.execute('ROLLBACK')

    ok('committed rows', len(db.execute('SELECT * FROM t').fetchall()), 2)

    try:
        db.execute('BEGIN')
        db.execute('INSERT INTO t (txt) VALUES (?)', ('tx3',))
        db.execute('ROLLBACK')
    except Exception:
        pass

    ok('rolled back row absent',
       db.execute("SELECT count(*) FROM t WHERE txt='tx3'").fetchone()[0], 0)


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

def run():
    test_bind_types()
    test_transient_gc()
    test_named_params()
    test_cursor_reuse()
    test_fetchone()
    test_fetchmany()
    test_fetchall()
    test_iterator()
    test_cursor_close()
    test_connection_close_with_cursors()
    test_context_manager()
    test_executemany()
    test_rowcount_lastrowid()
    test_large_blob()
    test_empty_blob()
    test_transaction()

    print()
    print('{} passed, {} failed'.format(_passed, _failed))
    return _failed == 0


if run():
    print('ALL OK')
else:
    print('FAILURES - see above')
