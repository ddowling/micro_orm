# micro_orm

A minimal SQLite ORM for MicroPython, designed for resource-constrained embedded targets such as the Raspberry Pi Pico. Works with [usqlite](https://github.com/spatialdude/usqlite) on MicroPython and falls back to the standard `sqlite3` module on CPython, so models can be developed and tested on a desktop.

## Requirements

- MicroPython with [usqlite](https://github.com/spatialdude/usqlite), **or**
- CPython 3.x (standard `sqlite3` module)

Copy `orm.py` to the device filesystem (or include it in a frozen module build).

## Quick start

```python
import usqlite as sqlite          # or: import sqlite3 as sqlite
from orm import model, Model, IntField, RealField, TextField, ForeignKeyField

db = sqlite.connect('mydata.db')
Model.set_db(db)

@model
class Cycle(Model):
    id         = IntField(primary_key=True)
    cell_id    = IntField(nullable=False)
    started_at = IntField()
    energy_mwh = RealField()

@model(table='charge_log')
class ChargeLog(Model):
    id         = IntField(primary_key=True)
    ts         = IntField(index=True)
    cycle_id   = ForeignKeyField(Cycle)
    voltage_mv = IntField()
    current_ma = IntField()
    temp_c     = RealField()

Cycle.create_table()
ChargeLog.create_table()
ChargeLog.create_indexes()

cyc = Cycle(cell_id=1, started_at=1000).insert()
print(cyc.id)           # auto-populated primary key

cyc.energy_mwh = 12.5
cyc.update()

row = Cycle.get(id=cyc.id)
rows = Cycle.filter(cell_id=1)

cyc.delete()
```

## Field types

| Field | SQLite type | Notes |
|---|---|---|
| `IntField` | `INTEGER` | |
| `RealField` | `REAL` | |
| `TextField` | `TEXT` | |
| `BlobField` | `BLOB` | Round-trips `bytes` objects |
| `BoolField` | `INTEGER` | Stores `0`/`1`; decodes to `bool` |
| `TimestampField` | `REAL` | Stores a float (e.g. `time.time()`) |
| `JSONField` | `TEXT` | Encodes/decodes via `json.dumps`/`json.loads` |
| `ForeignKeyField(Model)` | `INTEGER` | Emits a `REFERENCES` clause |

All field types accept these keyword arguments:

| Argument | Default | Meaning |
|---|---|---|
| `primary_key` | `False` | Marks the PK; INTEGER PK gets `AUTOINCREMENT` |
| `nullable` | `True` | Adds `NOT NULL` when `False` |
| `default` | `None` | Scalar default stored in schema; callable (e.g. `time.time`) evaluated at object construction and omitted from schema |
| `index` | `False` | Creates an index via `create_indexes()` |
| `old_name` | `None` | Previous column name, used by `migrate()` to carry data across a rename |

## The `@model` decorator

```python
@model                              # table name = class name lowercased
class Sensor(Model): ...

@model(table='cfg')                 # explicit table name
class Config(Model): ...

@model(table='sensor', old_name='raw_sensor')   # table was renamed
class Sensor(Model): ...
```

`@model` discovers `Field` attributes, records the table name and optional old name, and registers the class in a global registry (used by `ForeignKeyField` string references).

## Model API

### Class methods

```python
MyModel.set_db(db)          # set the shared database connection (call once)
MyModel.create_table()      # CREATE TABLE IF NOT EXISTS
MyModel.create_indexes()    # CREATE INDEX IF NOT EXISTS for index=True fields
MyModel.migrate()           # reconcile live schema with model definition (see below)
MyModel.get(field=value)    # SELECT … LIMIT 1, returns instance or None
MyModel.filter(field=value) # SELECT …, returns list (no kwargs = all rows)
```

### Instance methods

```python
obj = MyModel(field=value, ...)   # construct (defaults applied)
obj.insert()                      # INSERT, populates primary key, returns self
obj.update()                      # UPDATE by primary key
obj.delete()                      # DELETE by primary key
```

## Schema migration

`migrate()` compares the live SQLite schema against the current model definition and applies the minimum changes needed:

| Situation | Action |
|---|---|
| Table does not exist, no `old_name` | `create_table()` |
| Table does not exist, `old_name` table exists | create new table, copy matching columns, drop old table |
| Schema matches model exactly | no-op |
| Column added, dropped, renamed, or type changed | full table rebuild (create tmp → copy → drop old → create final → copy → drop tmp) |

Data is preserved across all migrations. Columns present in the database but absent from the model are dropped. New columns receive `NULL` (or the field default).

```python
# Startup sequence for an evolving application:
for cls in (Config, Cycle, ChargeLog):
    cls.migrate()
    cls.create_indexes()
```

**Column rename** — set `old_name` on the field:

```python
@model
class Metric(Model):
    id    = IntField(primary_key=True)
    value = IntField(old_name='val')   # was 'val' in the previous release
```

**Table rename** — set `old_name` on the decorator:

```python
@model(table='sensor', old_name='raw_sensor')
class Sensor(Model):
    ...
```

## BulkLogger

Buffers rows in memory and flushes them in batches for high-frequency logging (e.g. per-sample telemetry):

```python
from orm import BulkLogger

logger = BulkLogger(ChargeLog, flush_every=50)

# In your sample loop:
logger.log(ts=now, cycle_id=cyc.id, voltage_mv=v, current_ma=i, temp_c=t, state=s)

# At shutdown or periodically:
logger.flush()
```

Each `log()` call buffers one row. When the buffer reaches `flush_every` rows a single batched `INSERT` is issued. Unflushed rows are lost on reset, so call `flush()` at safe checkpoints.

## Embedded target notes

On a Raspberry Pi Pico (264 KB RAM) with usqlite, set these pragmas before creating any tables:

```python
db = sqlite.connect('mydata.db')
db.execute('PRAGMA page_size=512').close()   # minimum page size; reduces page-cache footprint
db.execute('PRAGMA cache_size=10').close()   # cap cache at 10 pages (5 KB)
Model.set_db(db)
```

## Running the tests

On CPython:

```
python3 test_orm.py
python3 test_usqlite.py
```

On MicroPython / Pico (via mpremote):

```
mpremote cp orm.py :orm.py + cp test_orm.py :test_orm.py
mpremote run test_orm.py
```
