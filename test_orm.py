# Use usqlite on micropython but also support running with sqlite3
# when using regular cpython
try:
    import usqlite as sqlite
except ImportError:
    import sqlite3 as sqlite

# Import orm classes
from orm import model, Model, IntField, RealField, TextField, ForeignKeyField, BulkLogger

# Connect to the database and set orm.Model to use this
db = sqlite.connect(':memory:')
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
    ts         = IntField()
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
    ok('fk references in schema', 'REFERENCES cycle (id)' in schema, True)

    # --- insert populates primary key ---
    cfg = Config(key='v_cutoff', value='4200', dtype='int').insert()
    ok('insert returns self',       type(cfg).__name__, 'Config')
    ok('insert populates id',       cfg.id, 1)

    # --- get returns matching row ---
    row = Config.get(key='v_cutoff')
    ok('get finds row',             row is not None, True)
    ok('get field value',           row.value, '4200')
    ok('get primary key',           row.id, 1)

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

    # --- delete removes row ---
    cyc2.delete()
    ok('delete removes row',        Cycle.get(id=cyc2.id), None)
    ok('delete leaves others',      Cycle.get(id=cyc.id) is not None, True)

    # --- repr ---
    r = repr(Config.get(key='v_cutoff'))
    ok('repr contains class name',  r.startswith('Config('), True)

    print()
    print('{} passed, {} failed'.format(passed, failed))
    return failed == 0


if run():
    print('ALL OK')
else:
    print('FAILURES - see above')
