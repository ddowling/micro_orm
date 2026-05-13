try:
    import usqlite as sqlite
    print("Using embedded usqlite")
except ImportError:
    import sqlite3 as sqlite
    print("Using default sqlite")

# --- Field descriptors ---

class Field:
    def __init__(self, sql_type, primary_key=False, nullable=True, default=None):
        self.sql_type = sql_type
        self.primary_key = primary_key
        self.nullable = nullable
        self.default = default
        self.name = None  # set by @model decorator

class IntField(Field):
    def __init__(self, **kw): super().__init__('INTEGER', **kw)

class RealField(Field):
    def __init__(self, **kw): super().__init__('REAL', **kw)

class TextField(Field):
    def __init__(self, **kw): super().__init__('TEXT', **kw)


# --- @model decorator: discovers fields, sets _table ---
# Usable as @model or @model(table='name')

def model(cls=None, table=None):
    def decorator(c):
        fields = {}
        for k, v in c.__dict__.items():
            if isinstance(v, Field):
                v.name = k
                fields[k] = v
        c._fields = fields
        c._table = table if table is not None else c.__name__.lower()
        return c
    if cls is not None:
        return decorator(cls)
    return decorator


# --- Base model (plain class, no metaclass) ---

class Model:
    _fields = {}
    _table  = ''
    _db     = None

    @classmethod
    def set_db(cls, db):
        cls._db = db

    def __init__(self, **kwargs):
        for name, field in self.__class__._fields.items():
            setattr(self, name, kwargs.get(name, field.default))

    def __repr__(self):
        parts = ['{}={!r}'.format(k, getattr(self, k, None)) for k in self.__class__._fields]
        return '{}({})'.format(self.__class__.__name__, ', '.join(parts))

    @classmethod
    def create_table(cls):
        cols = []
        for name, field in cls._fields.items():
            col = name + ' ' + field.sql_type
            if field.primary_key:
                col += ' PRIMARY KEY'
                if field.sql_type == 'INTEGER':
                    col += ' AUTOINCREMENT'
            elif not field.nullable:
                col += ' NOT NULL'
            if field.default is not None:
                col += ' DEFAULT ' + repr(field.default)
            cols.append(col)
        sql = 'CREATE TABLE IF NOT EXISTS {} ({})'.format(cls._table, ', '.join(cols))
        cls._db.execute(sql)
        _commit(cls._db)

    def insert(self):
        cls = self.__class__
        cols = [k for k, f in cls._fields.items() if not f.primary_key]
        vals = [getattr(self, k) for k in cols]
        placeholders = ', '.join('?' * len(cols))
        sql = 'INSERT INTO {} ({}) VALUES ({})'.format(
            cls._table, ', '.join(cols), placeholders)
        cur = cls._db.execute(sql, vals)
        for name, field in cls._fields.items():
            if field.primary_key:
                setattr(self, name, cur.lastrowid)
                break
        _commit(cls._db)
        return self

    def update(self):
        cls = self.__class__
        pk_name = _pk(cls)
        cols = [k for k in cls._fields if k != pk_name]
        vals = [getattr(self, c) for c in cols]
        vals.append(getattr(self, pk_name))
        set_clause = ', '.join(c + ' = ?' for c in cols)
        sql = 'UPDATE {} SET {} WHERE {} = ?'.format(cls._table, set_clause, pk_name)
        cls._db.execute(sql, vals)
        _commit(cls._db)
        return self

    def delete(self):
        cls = self.__class__
        pk_name = _pk(cls)
        sql = 'DELETE FROM {} WHERE {} = ?'.format(cls._table, pk_name)
        cls._db.execute(sql, [getattr(self, pk_name)])
        _commit(cls._db)

    @classmethod
    def get(cls, **kwargs):
        if not kwargs:
            raise ValueError('get() requires at least one keyword argument')
        field_names = list(cls._fields.keys())
        cols = ', '.join(field_names)
        where = ' AND '.join(k + ' = ?' for k in kwargs)
        vals = list(kwargs.values())
        sql = 'SELECT {} FROM {} WHERE {} LIMIT 1'.format(cols, cls._table, where)
        cur = cls._db.execute(sql, vals)
        row = cur.fetchone()
        if row is None:
            return None
        return _row_to_obj(cls, field_names, row)

    @classmethod
    def filter(cls, **kwargs):
        field_names = list(cls._fields.keys())
        cols = ', '.join(field_names)
        if kwargs:
            where = ' AND '.join(k + ' = ?' for k in kwargs)
            vals = list(kwargs.values())
            sql = 'SELECT {} FROM {} WHERE {}'.format(cols, cls._table, where)
        else:
            sql = 'SELECT {} FROM {}'.format(cols, cls._table)
            vals = []
        cur = cls._db.execute(sql, vals)
        results = []
        for row in cur.fetchall():
            results.append(_row_to_obj(cls, field_names, row))
        return results


# --- Helpers ---

def _pk(cls):
    for k, f in cls._fields.items():
        if f.primary_key:
            return k
    raise ValueError('No primary key defined on {}'.format(cls.__name__))

def _row_to_obj(cls, field_names, row):
    obj = object.__new__(cls)
    for i, name in enumerate(field_names):
        setattr(obj, name, row[i])
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
        cols = ', '.join(field_names)
        placeholders = ', '.join('?' * len(field_names))
        sql = 'INSERT INTO {} ({}) VALUES ({})'.format(
            self._cls._table, cols, placeholders)
        rows = [[row.get(k) for k in field_names] for row in self._buffer]
        try:
            db.executemany(sql, rows)
        except (AttributeError, TypeError):
            for row in rows:
                db.execute(sql, row)
        self._buffer = []
        _commit(db)
