import json
import sqlite3
import time
import uuid

from .config import VAR
from .io import clean

DB = VAR / "db/dataqc.sqlite3"


def connect():
    DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init():
    with connect() as c:
        c.executescript("""CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, root TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL, config TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, error TEXT NOT NULL DEFAULT '', exports TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS episodes(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id), number INTEGER NOT NULL, root TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', grade TEXT, reason TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0, UNIQUE(run_id,number));
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, episode_id INTEGER, action TEXT NOT NULL, actor TEXT NOT NULL, before TEXT, after TEXT, created REAL NOT NULL);""")


def unpack(row):
    if row is None:
        return None
    d = dict(row)
    for k in ("config", "exports", "data"):
        if k in d:
            d[k] = json.loads(d[k])
    return d


def get_run(i):
    with connect() as c:
        return unpack(c.execute("SELECT * FROM runs WHERE id=?", (i,)).fetchone())


def runs():
    with connect() as c:
        return [
            unpack(r) for r in c.execute("SELECT * FROM runs ORDER BY created DESC")
        ]


def episodes(i):
    with connect() as c:
        return [
            unpack(r)
            for r in c.execute(
                "SELECT * FROM episodes WHERE run_id=? ORDER BY number", (i,)
            )
        ]


def episode(i):
    with connect() as c:
        return unpack(c.execute("SELECT * FROM episodes WHERE id=?", (i,)).fetchone())


def update(table, i, **values):
    allowed = {
        "runs": {"status", "phase", "config", "updated", "error", "exports"},
        "episodes": {"status", "grade", "reason", "data", "revision"},
    }
    if table not in allowed or not set(values) <= allowed[table]:
        raise ValueError("非法更新")
    values = {
        k: json.dumps(clean(v), ensure_ascii=False)
        if isinstance(v, (dict, list))
        else v
        for k, v in values.items()
    }
    with connect() as c:
        c.execute(
            f"UPDATE {table} SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
            (*values.values(), i),
        )


def audit(run, ep, action, actor, before, after):
    with connect() as c:
        c.execute(
            "INSERT INTO audit(run_id,episode_id,action,actor,before,after,created) VALUES(?,?,?,?,?,?,?)",
            (
                run,
                ep,
                action,
                actor,
                json.dumps(clean(before), ensure_ascii=False),
                json.dumps(clean(after), ensure_ascii=False),
                time.time(),
            ),
        )


def create(root, mode, cfg, paths):
    ident = uuid.uuid4().hex[:12]
    now = time.time()
    with connect() as c:
        c.execute(
            "INSERT INTO runs(id,root,mode,status,phase,config,created,updated) VALUES(?,?,?,?,?,?,?,?)",
            (ident, root, mode, "queued", "等待处理", json.dumps(cfg), now, now),
        )
        c.executemany(
            "INSERT INTO episodes(run_id,number,root) VALUES(?,?,?)",
            [(ident, i, p) for i, p in enumerate(paths)],
        )
    return ident
