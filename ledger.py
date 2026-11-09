"""Append-only SHA-256 evidence chain stored in SQLite.

Writes run inside BEGIN IMMEDIATE so concurrent requests cannot fork the chain.
This is a local tamper-evident log: it detects edits, deletions and reordering,
but it is not externally anchored or signed.
"""
import hashlib, json, sqlite3, time

GENESIS = "0" * 64


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(event, payload, ts, previous):
    return hashlib.sha256(canon({"event": event, "payload": payload, "time": ts,
                                 "previous": previous}).encode()).hexdigest()


def init(c: sqlite3.Connection):
    c.execute("CREATE TABLE IF NOT EXISTS evidence (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
              " event TEXT NOT NULL, payload TEXT NOT NULL, ts INTEGER NOT NULL,"
              " previous TEXT NOT NULL, hash TEXT NOT NULL)")


def append(c: sqlite3.Connection, event, payload):
    """Caller must hold a write transaction (BEGIN IMMEDIATE)."""
    last = c.execute("SELECT hash FROM evidence ORDER BY seq DESC LIMIT 1").fetchone()
    previous = last[0] if last else GENESIS
    ts = int(time.time() * 1000)
    h = entry_hash(event, payload, ts, previous)
    c.execute("INSERT INTO evidence(event,payload,ts,previous,hash) VALUES(?,?,?,?,?)",
              (event, canon(payload), ts, previous, h))
    return h


def entries(c):
    rows = c.execute("SELECT seq,event,payload,ts,previous,hash FROM evidence ORDER BY seq").fetchall()
    return [dict(seq=r[0], event=r[1], payload=json.loads(r[2]), time=r[3], previous=r[4], hash=r[5])
            for r in rows]


def verify(c):
    previous, n = GENESIS, 0
    for e in entries(c):
        n += 1
        if e["previous"] != previous or entry_hash(e["event"], e["payload"], e["time"], previous) != e["hash"]:
            return {"intact": False, "entries": n, "broken_at_seq": e["seq"]}
        previous = e["hash"]
    return {"intact": True, "entries": n, "head": previous}
