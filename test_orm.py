# Use usqlite on micropython but also support running with sqlite3
# when using regular cpython
try:
    import usqlite as sqlite
except ImportError:
    import sqlite3 as sqlite

# Import orm classes
import time
from orm import model, Model, IntField, RealField, TextField, TimestampField, \
                  BlobField, BoolField, JSONField, ForeignKeyField, BulkLogger

# Connect to the database and set orm.Model to use this
db = sqlite.connect(':memory:')
# Reduce page size before any tables are created (512 is the minimum).
db.execute('PRAGMA page_size=512').close()
# Cap page cache to 10 pages (5 KB) to limit peak heap usage on embedded targets.
db.execute('PRAGMA cache_size=10').close()
Model.set_db(db)

# Each model is defined be inherriting from Model and adding the @model decorator
@model(table='config')
class Config(Model):
    id    = IntField(primary_key=True)
    key   = TextField(nullable=False)
    value = TextField()
    dtype = TextField()

@model
class Cycle(Model):
    id          = IntField(primary_key=True)
    cell_id     = IntField(nullable=False)
    started_at  = IntField()
    ended_at    = IntField()
    energy_mwh  = RealField()
    termination = TextField()

@model(table='charge_log')
class ChargeLog(Model):
    id         = IntField(primary_key=True)
    ts         = IntField(index=True)
    cycle_id   = ForeignKeyField(Cycle)
    voltage_mv = IntField()
    current_ma = IntField()
    temp_c     = RealField()
    state      = IntField()


def check(desc, got, expected):
    if got != expected:
        print('FAIL:', desc)
        print('  expected:', expected)
        print('  got:     ', got)
        return False
    print('ok  ', desc)
    return True


def run():
    passed = 0
    failed = 0

    def ok(desc, got, expected):
        nonlocal passed, failed
        if check(desc, got, expected):
            passed += 1
        else:
            failed += 1

    # --- create tables ---
    for cls in (Config, Cycle, ChargeLog):
        cls.create_table()

    # --- foreign key schema constraint ---
    cur = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='charge_log'")
    schema = cur.fetchone()[0]
    cur.close()
    ok('fk references in schema', 'REFERENCES "cycle" ("id")' in schema, True)

    # --- index field attribute ---
    ok('pk implies index',           ChargeLog._fields['id'].index,         True)
    ok('index=True stored',          ChargeLog._fields['ts'].index,         True)
    ok('index defaults False',       ChargeLog._fields['voltage_mv'].index, False)

    # --- create_indexes creates index in sqlite_master ---
    for cls in (Config, Cycle, ChargeLog):
        cls.create_indexes()
    cur = db.execute("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='charge_log'")
    index_names = [r[0] for r in cur.fetchall()]
    cur.close()
    ok('ts index created',           'idx_charge_log_ts' in index_names,    True)
    ok('pk not re-indexed',          'idx_charge_log_id' in index_names,    False)
    ok('non-indexed field skipped',  'idx_charge_log_voltage_mv' in index_names, False)

    # --- insert populates primary key ---
    cfg = Config(key='v_cutoff', value='4200', dtype='int').insert()
    ok('insert returns self',       type(cfg).__name__, 'Config')
    ok('insert populates id',       cfg.id, 1)

    # --- get returns matching row ---
    row = Config.get(key='v_cutoff')
    ok('get finds row',             row is not None, True)
    ok('get field value',           row.value, '4200')
    ok('get primary key',           row.id, 1)

    # --- get with multiple field=value args (implicit AND) ---
    ok('get multi-arg match',        Config.get(key='v_cutoff', dtype='int') is not None, True)
    ok('get multi-arg no match',     Config.get(key='v_cutoff', dtype='str'), None)

    # --- get returns None for missing ---
    ok('get missing returns None',  Config.get(key='nope'), None)

    # --- update persists changes ---
    row.value = '4150'
    row.update()
    ok('update persists',           Config.get(key='v_cutoff').value, '4150')

    # --- filter with no args returns all rows ---
    Config(key='i_charge', value='500', dtype='int').insert()
    ok('filter all count',          len(Config.filter()), 2)

    # --- filter with kwargs narrows results ---
    ok('filter by field count',     len(Config.filter(dtype='int')), 2)
    ok('filter no match count',     len(Config.filter(dtype='str')), 0)

    # --- second model: cycle ---
    cyc = Cycle(cell_id=1, started_at=1000).insert()
    ok('cycle insert id',           cyc.id, 1)

    cyc2 = Cycle(cell_id=2, started_at=2000).insert()
    ok('second cycle id',           cyc2.id, 2)
    ok('filter by cell_id',         len(Cycle.filter(cell_id=1)), 1)

    # --- update non-pk fields ---
    cyc.ended_at = 9999
    cyc.energy_mwh = 12.5
    cyc.termination = 'cv_cutoff'
    cyc.update()
    fetched = Cycle.get(id=cyc.id)
    ok('update ended_at',           fetched.ended_at, 9999)
    ok('update energy_mwh',         fetched.energy_mwh, 12.5)
    ok('update termination',        fetched.termination, 'cv_cutoff')

    # --- BulkLogger auto-flushes at threshold ---
    logger = BulkLogger(ChargeLog, flush_every=3)
    for i in range(7):
        logger.log(ts=1000 + i, cycle_id=cyc.id,
                   voltage_mv=4100 + i, current_ma=500,
                   temp_c=25.0, state=0)
    # 6 rows flushed in two batches of 3, 1 still buffered
    ok('bulklogger buffered count', len(logger._buffer), 1)
    logger.flush()
    ok('bulklogger total rows',     len(ChargeLog.filter(cycle_id=cyc.id)), 7)

    # --- filter: order, limit, offset ---
    # ChargeLog has 7 rows with ts = 1000..1006
    asc  = ChargeLog.filter(order='ts')
    ok('order asc first',           asc[0].ts,  1000)
    ok('order asc last',            asc[-1].ts, 1006)

    desc = ChargeLog.filter(order='-ts')
    ok('order desc first',          desc[0].ts, 1006)
    ok('order desc last',           desc[-1].ts, 1000)

    expl = ChargeLog.filter(order='+ts')
    ok('order +prefix asc first',   expl[0].ts, 1000)

    top3 = ChargeLog.filter(order='-ts', limit=3)
    ok('limit count',               len(top3),   3)
    ok('limit+order first',         top3[0].ts,  1006)
    ok('limit+order last',          top3[2].ts,  1004)

    page = ChargeLog.filter(order='ts', limit=3, offset=2)
    ok('offset start',              page[0].ts,  1002)
    ok('offset count',              len(page),   3)

    multi = ChargeLog.filter(order=['-voltage_mv', 'ts'], limit=2)
    ok('multi-order count',         len(multi),  2)
    ok('multi-order first voltage', multi[0].voltage_mv, 4106)

    # --- filter: tuple operator values ---
    # ChargeLog has ts=1000..1006, voltage_mv=4100..4106
    ok('tuple >=',   len(ChargeLog.filter(ts=('>=', 1004))),          3)
    ok('tuple >',    len(ChargeLog.filter(ts=('>', 1004))),            2)
    ok('tuple <=',   len(ChargeLog.filter(ts=('<=', 1002))),          3)
    ok('tuple <',    len(ChargeLog.filter(ts=('<', 1002))),            2)
    ok('tuple !=',   len(ChargeLog.filter(ts=('!=', 1003))),          6)
    ok('tuple range', len(ChargeLog.filter(ts=('>=', 1002), voltage_mv=('<=', 4104))), 3)
    ok('tuple + eq',  len(ChargeLog.filter(cycle_id=cyc.id, ts=('>=', 1005))),         2)

    # --- filter: raw where expression ---
    ok('where string',
       len(ChargeLog.filter(where='ts >= 1002 AND ts <= 1004')),                       3)
    ok('where tuple params',
       len(ChargeLog.filter(where=('ts >= ? AND ts <= ?', [1002, 1004]))),             3)
    ok('where + kwargs',
       len(ChargeLog.filter(cycle_id=cyc.id, where=('ts >= ?', [1003]))),             4)

    # --- delete removes row ---
    cyc2.delete()
    ok('delete removes row',        Cycle.get(id=cyc2.id), None)
    ok('delete leaves others',      Cycle.get(id=cyc.id) is not None, True)

    # --- repr ---
    r = repr(Config.get(key='v_cutoff'))
    ok('repr contains class name',  r.startswith('Config('), True)

    # --- TimestampField ---
    @model(table='event_ts')
    class EventTs(Model):
        id         = IntField(primary_key=True)
        created_at = TimestampField(default=time.time)
        ended_at   = TimestampField()

    EventTs.create_table()

    before = time.time()
    ev = EventTs().insert()
    after = time.time()

    ok('timestamp default callable',   before <= ev.created_at <= after, True)
    ok('timestamp no default is None', ev.ended_at,                      None)
    ok('timestamp stored as real', type(ev.created_at) in (float, int),  True)

    ev2 = EventTs.get(id=ev.id)
    ok('timestamp round-trips',        before <= ev2.created_at <= after, True)

    # callable default not in SQL schema
    cur = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='event_ts'")
    ts_schema = cur.fetchone()[0]
    cur.close()
    ok('callable default not in schema', 'DEFAULT' not in ts_schema, True)

    # --- BlobField, BoolField, JSONField ---
    @model(table='typed')
    class Typed(Model):
        id      = IntField(primary_key=True)
        raw     = BlobField()
        active  = BoolField(default=False)
        meta    = JSONField()

    Typed.create_table()

    payload = bytes([0x01, 0x02, 0xFF])
    rec = Typed(raw=payload, active=True, meta={'v': 1, 'tags': ['a', 'b']}).insert()

    ok('blob round-trips',         Typed.get(id=rec.id).raw,    payload)
    ok('blob type is bytes',       type(Typed.get(id=rec.id).raw), bytes)

    ok('bool True round-trips',    Typed.get(id=rec.id).active, True)
    ok('bool type is bool',        type(Typed.get(id=rec.id).active), bool)

    rec2 = Typed(raw=b'', active=False, meta=None).insert()
    ok('bool False round-trips',   Typed.get(id=rec2.id).active, False)

    ok('json round-trips',         Typed.get(id=rec.id).meta,   {'v': 1, 'tags': ['a', 'b']})
    ok('json type is dict',        type(Typed.get(id=rec.id).meta), dict)
    ok('json None round-trips',    Typed.get(id=rec2.id).meta,  None)

    # bool default=False encodes to DEFAULT 0 in schema, not DEFAULT False
    cur = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='typed'")
    typed_schema = cur.fetchone()[0]
    cur.close()
    ok('bool default encoded in schema', 'DEFAULT 0' in typed_schema, True)

    # raw stored value for bool is integer 0/1
    cur = db.execute('SELECT active FROM "typed" WHERE id=?', [rec.id])
    ok('bool stored as integer',   cur.fetchone()[0], 1)
    cur.close()

    # raw stored value for json is a string
    cur = db.execute('SELECT meta FROM "typed" WHERE id=?', [rec.id])
    raw_meta = cur.fetchone()[0]
    cur.close()
    ok('json stored as string',    isinstance(raw_meta, str), True)

    # --- migrate helpers ---
    def tbl_exists(name):
        cur = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", [name])
        found = cur.fetchone() is not None
        cur.close()
        return found

    def col_names(tbl):
        cur = db.execute('PRAGMA table_info({})'.format(tbl))
        names = [r[1] for r in cur.fetchall()]
        cur.close()
        return names

    # --- migrate: no-op when schema matches ---
    Config.migrate()
    ok('migrate no-op preserves rows', len(Config.filter()), 2)

    # --- migrate: table rename ---
    db.execute('CREATE TABLE old_sensor (id INTEGER PRIMARY KEY AUTOINCREMENT, val REAL NOT NULL)').close()
    db.execute("INSERT INTO old_sensor (val) VALUES (1.5)").close()

    @model(table='sensor', old_name='old_sensor')
    class Sensor(Model):
        id  = IntField(primary_key=True)
        val = RealField(nullable=False)

    Sensor.migrate()
    ok('table rename: new name exists',  tbl_exists('sensor'),     True)
    ok('table rename: old name gone',    tbl_exists('old_sensor'), False)
    ok('table rename: data preserved',   len(Sensor.filter()),     1)

    # --- migrate: new column added ---
    db.execute('CREATE TABLE reading (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER)').close()
    db.execute("INSERT INTO reading (ts) VALUES (1000)").close()

    @model(table='reading')
    class Reading(Model):
        id    = IntField(primary_key=True)
        ts    = IntField()
        units = TextField()

    Reading.migrate()
    ok('new column: present in schema',  'units' in col_names('reading'), True)
    ok('new column: existing row intact', Reading.filter()[0].ts,         1000)
    ok('new column: default is None',     Reading.filter()[0].units,      None)

    # --- migrate: column dropped ---
    db.execute('CREATE TABLE event (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, raw INTEGER)').close()
    db.execute("INSERT INTO event (ts, raw) VALUES (42, 99)").close()

    @model(table='event')
    class Event(Model):
        id = IntField(primary_key=True)
        ts = IntField()

    Event.migrate()
    ok('drop column: removed from schema', 'raw' not in col_names('event'), True)
    ok('drop column: kept column intact',   Event.filter()[0].ts,           42)

    # --- migrate: column renamed ---
    db.execute('CREATE TABLE metric (id INTEGER PRIMARY KEY AUTOINCREMENT, val INTEGER)').close()
    db.execute("INSERT INTO metric (val) VALUES (77)").close()

    @model(table='metric')
    class Metric(Model):
        id    = IntField(primary_key=True)
        value = IntField(old_name='val')

    Metric.migrate()
    ok('rename column: new name in schema', 'value' in col_names('metric'), True)
    ok('rename column: old name gone',      'val'   not in col_names('metric'), True)
    ok('rename column: data preserved',     Metric.filter()[0].value, 77)

    # --- migrate: column type change ---
    db.execute('CREATE TABLE sample (id INTEGER PRIMARY KEY AUTOINCREMENT, reading INTEGER)').close()
    db.execute("INSERT INTO sample (reading) VALUES (3)").close()

    @model(table='sample')
    class Sample(Model):
        id      = IntField(primary_key=True)
        reading = RealField()

    Sample.migrate()
    ok('type change: schema updated', col_names('sample'), ['id', 'reading'])
    cur = db.execute('PRAGMA table_info(sample)')
    ok('type change: new type stored',
       next(r[2] for r in cur.fetchall() if r[1] == 'reading'), 'REAL')
    cur.close()

    print()
    print('{} passed, {} failed'.format(passed, failed))
    return failed == 0


if run():
    print('ALL OK')
else:
    print('FAILURES - see above')
