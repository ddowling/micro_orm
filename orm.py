try:
    import usqlite as sqlite
    print("Using embedded usqlite")
except ImportError:
    import sqlite3 as sqlite
    print("Using default sqlite")

try:
    import ujson as json
except ImportError:
    import json

try:
    import gc as _gc
except ImportError:
    _gc = None

# --- Field descriptors ---

class Field:
    def __init__(self, sql_type, primary_key=False, nullable=True, default=None, index=False, old_name=None):
        self.sql_type = sql_type
        self.primary_key = primary_key
        self.nullable = nullable
        self.default = default
        self.index = index or primary_key
        self.old_name = old_name
        self.name = None  # set by @model decorator

    def encode(self, value):
        return value

    def decode(self, value):
        return value

class IntField(Field):
    def __init__(self, **kw): super().__init__('INTEGER', **kw)

class RealField(Field):
    def __init__(self, **kw): super().__init__('REAL', **kw)

class TextField(Field):
    def __init__(self, **kw): super().__init__('TEXT', **kw)

class TimestampField(Field):
    def __init__(self, **kw): super().__init__('REAL', **kw)

class BlobField(Field):
    def __init__(self, **kw): super().__init__('BLOB', **kw)

class BoolField(Field):
    def __init__(self, **kw): super().__init__('INTEGER', **kw)

    def encode(self, value):
        return None if value is None else (1 if value else 0)

    def decode(self, value):
        return None if value is None else bool(value)

class JSONField(Field):
    def __init__(self, **kw): super().__init__('TEXT', **kw)

    def encode(self, value):
        return None if value is None else json.dumps(value)

    def decode(self, value):
        return None if value is None else json.loads(value)

class ForeignKeyField(Field):
    def __init__(self, related, **kw):
        super().__init__('INTEGER', **kw)
        self.related = related  # model class or string name for forward refs

    def resolve(self):
        if isinstance(self.related, str):
            return _registry[self.related]
        return self.related


# --- Model registry (populated by @model, used by ForeignKeyField.resolve) ---

_registry = {}


# --- @model decorator: discovers fields, sets _table ---
# Usable as @model or @model(table='name')

def model(cls=None, table=None, old_name=None):
    def decorator(c):
        fields = {}
        for k, v in c.__dict__.items():
            if isinstance(v, Field):
                v.name = k
                fields[k] = v
        c._fields = fields
        c._table = table if table is not None else c.__name__.lower()
        c._old_name = old_name
        _registry[c.__name__] = c
        return c
    if cls is not None:
        return decorator(cls)
    return decorator


# --- Base model (plain class, no metaclass) ---

class Model:
    _fields   = {}
    _table    = ''
    _old_name = None
    _db       = None

    @classmethod
    def set_db(cls, db):
        cls._db = db

    def __init__(self, **kwargs):
        for name, field in self.__class__._fields.items():
            if name in kwargs:
                setattr(self, name, kwargs[name])
            else:
                d = field.default
                setattr(self, name, d() if callable(d) else d)

    def __repr__(self):
        parts = ['{}={!r}'.format(k, getattr(self, k, None)) for k in self.__class__._fields]
        return '{}({})'.format(self.__class__.__name__, ', '.join(parts))

    @classmethod
    def create_table(cls):
        cls._db.execute(_build_table_sql(cls)).close()
        _commit(cls._db)

    @classmethod
    def create_indexes(cls):
        for name, field in cls._fields.items():
            if field.index and not field.primary_key:
                idx = 'idx_{}_{}'.format(cls._table, name)
                sql = 'CREATE INDEX IF NOT EXISTS {} ON {} ({})'.format(
                    _qi(idx), _qi(cls._table), _qi(name))
                cls._db.execute(sql).close()
        _commit(cls._db)

    @classmethod
    def migrate(cls):
        db = cls._db
        table = cls._table

        if not _table_exists(db, table):
            if cls._old_name and _table_exists(db, cls._old_name):
                # DO NOT replace this with:
                #   db.execute('ALTER TABLE old RENAME TO new')
                #
                # SQLite 3.47+ ALTER TABLE RENAME rewrites every schema object
                # that references the old table name.  On MicroPython the SQLite
                # allocator routes through gc_alloc, so that rewrite triggers
                # enough heap pressure to cause an automatic gc.collect().
                # MicroPython's conservative GC then frees live SQLite pages
                # whose pointers it cannot trace, corrupting the in-memory
                # database (SQLITE_CORRUPT / HardFault on RP2040).
                # Verified on usqlite with MicroPython 1.24 / SQLite 3.47.
                old_col_names = set(_db_columns(db, cls._old_name).keys())
                col_list = [f for f in cls._fields if f in old_col_names]
                db.execute(_build_table_sql(cls)).close()
                if col_list:
                    db.execute('INSERT INTO {} ({}) SELECT {} FROM {}'.format(
                        _qi(table),
                        ', '.join(_qi(c) for c in col_list),
                        ', '.join(_qi(c) for c in col_list),
                        _qi(cls._old_name))).close()
                db.execute('DROP TABLE {}'.format(_qi(cls._old_name))).close()
                _commit(db)
            else:
                cls.create_table()
            return

        old_cols = _db_columns(db, table)
        need_rebuild = False
        new_col_list = []
        old_col_list = []

        for fname, field in cls._fields.items():
            if fname in old_cols:
                if not _col_matches(field, old_cols[fname]):
                    need_rebuild = True
                new_col_list.append(fname)
                old_col_list.append(fname)
            else:
                need_rebuild = True
                src = field.old_name if (field.old_name and field.old_name in old_cols) else None
                if src is not None:
                    new_col_list.append(fname)
                    old_col_list.append(src)

        if not need_rebuild:
            mapped = set(old_col_list)
            for col_name in old_cols:
                if col_name not in mapped:
                    need_rebuild = True
                    break

        if not need_rebuild:
            return

        # Rebuild sequence: create tmp, copy old→tmp, drop old, create final,
        # copy tmp→final, drop tmp.
        #
        # DO NOT simplify the last two steps to:
        #   ALTER TABLE tmp RENAME TO table
        #
        # Same reason as the rename path above: ALTER TABLE RENAME in SQLite
        # 3.47+ does a full schema rewrite that causes gc.collect() under heap
        # pressure on MicroPython.  That frees live SQLite pages (conservative
        # GC cannot trace all interior SQLite pointers), corrupting the database.
        # The symptom is SQLITE_CORRUPT followed by a HardFault in pager_playback
        # during the attempted rollback (verified on RP2040 / usqlite).
        tmp = table + '_tmp'
        _commit(db)
        db.execute('BEGIN')
        try:
            db.execute('DROP TABLE IF EXISTS {}'.format(_qi(tmp))).close()
            db.execute(_build_table_sql(cls, tmp)).close()
            if new_col_list:
                db.execute('INSERT INTO {} ({}) SELECT {} FROM {}'.format(
                    _qi(tmp),
                    ', '.join(_qi(c) for c in new_col_list),
                    ', '.join(_qi(c) for c in old_col_list),
                    _qi(table))).close()
            db.execute('DROP TABLE {}'.format(_qi(table))).close()
            db.execute(_build_table_sql(cls)).close()
            if new_col_list:
                db.execute('INSERT INTO {} ({}) SELECT {} FROM {}'.format(
                    _qi(table),
                    ', '.join(_qi(c) for c in new_col_list),
                    ', '.join(_qi(c) for c in new_col_list),
                    _qi(tmp))).close()
            db.execute('DROP TABLE {}'.format(_qi(tmp))).close()
            db.execute('COMMIT').close()
        except Exception:
            try:
                db.execute('ROLLBACK').close()
            except Exception:
                pass
            raise

    def insert(self):
        cls = self.__class__
        cols = [k for k, f in cls._fields.items() if not f.primary_key]
        vals = [cls._fields[k].encode(getattr(self, k)) for k in cols]
        placeholders = ', '.join('?' * len(cols))
        sql = 'INSERT INTO {} ({}) VALUES ({})'.format(
            _qi(cls._table), ', '.join(_qi(c) for c in cols), placeholders)
        cur = cls._db.execute(sql, vals)
        for name, field in cls._fields.items():
            if field.primary_key:
                setattr(self, name, cur.lastrowid)
                break
        cur.close()
        _commit(cls._db)
        return self

    def update(self):
        cls = self.__class__
        pk_name = _pk(cls)
        cols = [k for k in cls._fields if k != pk_name]
        vals = [cls._fields[c].encode(getattr(self, c)) for c in cols]
        vals.append(getattr(self, pk_name))
        set_clause = ', '.join(_qi(c) + ' = ?' for c in cols)
        sql = 'UPDATE {} SET {} WHERE {} = ?'.format(
            _qi(cls._table), set_clause, _qi(pk_name))
        cls._db.execute(sql, vals).close()
        _commit(cls._db)
        return self

    def delete(self):
        cls = self.__class__
        pk_name = _pk(cls)
        sql = 'DELETE FROM {} WHERE {} = ?'.format(_qi(cls._table), _qi(pk_name))
        cls._db.execute(sql, [getattr(self, pk_name)]).close()
        _commit(cls._db)

    @classmethod
    def get(cls, **kwargs):
        if not kwargs:
            raise ValueError('get() requires at least one keyword argument')
        field_names = list(cls._fields.keys())
        cols = ', '.join(_qi(f) for f in field_names)
        where = ' AND '.join(_qi(k) + ' = ?' for k in kwargs)
        vals = list(kwargs.values())
        sql = 'SELECT {} FROM {} WHERE {} LIMIT 1'.format(cols, _qi(cls._table), where)
        cur = cls._db.execute(sql, vals)
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None
        return _row_to_obj(cls, field_names, row)

    @classmethod
    def filter(cls, order=None, limit=None, offset=None, where=None, **kwargs):
        field_names = list(cls._fields.keys())
        cols = ', '.join(_qi(f) for f in field_names)
        parts = []
        vals = []
        for k, v in kwargs.items():
            if isinstance(v, tuple):
                parts.append(_qi(k) + ' ' + v[0] + ' ?')
                vals.append(v[1])
            else:
                parts.append(_qi(k) + ' = ?')
                vals.append(v)
        if where is not None:
            if isinstance(where, tuple):
                parts.append(where[0])
                vals.extend(where[1])
            else:
                parts.append(where)
        if parts:
            sql = 'SELECT {} FROM {} WHERE {}'.format(
                cols, _qi(cls._table), ' AND '.join(parts))
        else:
            sql = 'SELECT {} FROM {}'.format(cols, _qi(cls._table))
        if order is not None:
            if isinstance(order, str):
                order = [order]
            parts = []
            for col in order:
                if col.startswith('-'):
                    parts.append(_qi(col[1:]) + ' DESC')
                else:
                    parts.append(_qi(col.lstrip('+')) + ' ASC')
            sql += ' ORDER BY ' + ', '.join(parts)
        if limit is not None:
            sql += ' LIMIT {}'.format(int(limit))
        if offset is not None:
            sql += ' OFFSET {}'.format(int(offset))
        cur = cls._db.execute(sql, vals)
        rows = cur.fetchall()
        cur.close()
        results = []
        for row in rows:
            results.append(_row_to_obj(cls, field_names, row))
        return results


# --- Helpers ---

def _qi(name):
    return '"' + name.replace('"', '""') + '"'

def _build_table_sql(cls, tbl=None):
    if tbl is None:
        tbl = cls._table
    cols = []
    for fname, field in cls._fields.items():
        col = _qi(fname) + ' ' + field.sql_type
        if field.primary_key:
            col += ' PRIMARY KEY'
            if field.sql_type == 'INTEGER':
                col += ' AUTOINCREMENT'
        elif not field.nullable:
            col += ' NOT NULL'
        if field.default is not None and not callable(field.default):
            col += ' DEFAULT ' + repr(field.encode(field.default))
        if isinstance(field, ForeignKeyField):
            rel = field.resolve()
            col += ' REFERENCES {} ({})'.format(_qi(rel._table), _qi(_pk(rel)))
        cols.append(col)
    return 'CREATE TABLE IF NOT EXISTS {} ({})'.format(_qi(tbl), ', '.join(cols))

def _table_exists(db, name):
    cur = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", [name])
    found = cur.fetchone() is not None
    cur.close()
    return found

def _db_columns(db, table):
    cur = db.execute('PRAGMA table_info({})'.format(_qi(table)))
    rows = cur.fetchall()
    cur.close()
    return {row[1]: {'type': row[2], 'notnull': bool(row[3]),
                     'dflt_value': row[4], 'pk': bool(row[5])}
            for row in rows}

def _col_matches(field, db_col):
    if field.sql_type != db_col['type']:
        return False
    if bool(field.primary_key) != db_col['pk']:
        return False
    if (not field.nullable and not field.primary_key) != db_col['notnull']:
        return False
    expected = None if (field.default is None or callable(field.default)) else repr(field.encode(field.default))
    if expected != db_col['dflt_value']:
        return False
    return True

def _pk(cls):
    for k, f in cls._fields.items():
        if f.primary_key:
            return k
    raise ValueError('No primary key defined on {}'.format(cls.__name__))

def _row_to_obj(cls, field_names, row):
    obj = object.__new__(cls)
    for i, name in enumerate(field_names):
        setattr(obj, name, cls._fields[name].decode(row[i]))
    return obj

def _commit(db):
    try:
        db.commit()
    except Exception:
        pass


# --- BulkLogger: buffers rows and flushes in batches ---

class BulkLogger:
    def __init__(self, model_cls, flush_every=100):
        self._cls = model_cls
        self._flush_every = flush_every
        self._buffer = []

    def log(self, **kwargs):
        self._buffer.append(kwargs)
        if len(self._buffer) >= self._flush_every:
            self.flush()

    def flush(self):
        if not self._buffer:
            return
        db = self._cls._db
        field_names = [k for k, f in self._cls._fields.items() if not f.primary_key]
        cols = ', '.join(_qi(k) for k in field_names)
        placeholders = ', '.join('?' * len(field_names))
        sql = 'INSERT INTO {} ({}) VALUES ({})'.format(
            _qi(self._cls._table), cols, placeholders)
        rows = [[self._cls._fields[k].encode(row.get(k)) for k in field_names] for row in self._buffer]
        try:
            db.executemany(sql, rows).close()
        except (AttributeError, TypeError):
            for row in rows:
                db.execute(sql, row).close()
        self._buffer = []
        _commit(db)
