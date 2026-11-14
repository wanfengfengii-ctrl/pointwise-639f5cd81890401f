import json
import re
import sys
from pathlib import Path
from lake import Lake
from checks import check

TABLES = ("snapshots", "files", "tombstones", "head")


class Rows:
    def __init__(self, cursor, meter, table):
        self.cursor, self.meter, self.table = cursor, meter, table

    def note(self, rows):
        if self.table:
            self.meter.read[self.table] += len(rows)
        return rows

    def fetchone(self):
        row = self.cursor.fetchone()
        self.note([] if row is None else [row])
        return row

    def fetchall(self):
        return self.note(self.cursor.fetchall())

    def __iter__(self):
        return self

    def __next__(self):
        row = next(self.cursor)
        self.note([row])
        return row

    def __getattr__(self, name):
        return getattr(self.cursor, name)


class Meter:
    """Counts fetched SQL rows and affected DML rows, not VM work or page I/O."""
    def __init__(self, db):
        self.db = db
        self.reset()

    def reset(self):
        self.read = dict.fromkeys(TABLES, 0)
        self.write = dict.fromkeys(TABLES, 0)

    def call(self, method, sql, args):
        select = re.match(r"\s*SELECT\b", sql, re.I)
        pattern = r"\bFROM\s+(\w+)" if select else r"\b(?:INTO|UPDATE|FROM)\s+(\w+)"
        match = re.search(pattern, sql, re.I)
        table = match.group(1).lower() if match else None
        table = table if table in TABLES else None
        before = self.db.total_changes
        cursor = getattr(self.db, method)(sql, args)
        if not select and table:
            self.write[table] += self.db.total_changes - before
        return Rows(cursor, self, table if select else None)

    def execute(self, sql, args=()):
        return self.call("execute", sql, args)

    def executemany(self, sql, args):
        return self.call("executemany", sql, args)

    def __getattr__(self, name):
        return getattr(self.db, name)

    def counts(self):
        return {"read": dict(self.read), "write": dict(self.write)}


class RollbackProbe(Exception):
    pass


def logical(lake, raw):
    # Observer-only reads are explicitly outside the measured operation.
    wrapped = lake.catalog.db
    lake.catalog.db = raw
    try:
        sid, files, dead = lake.catalog.state()
        return {"head": sid, "files": sorted(files),
                "dead": [list(x) for x in sorted(dead)]}
    finally:
        lake.catalog.db = wrapped


def run(spec):
    lake = Lake()
    initial = lake.seed(spec["files"])
    raw = lake.catalog.db
    meter = Meter(raw)
    lake.catalog.db = meter
    result = {"initial": logical(lake, raw), "steps": []}
    plan = None
    try:
        for operation in spec["operations"]:
            meter.reset()
            op = operation["op"]
            record = {"op": op}
            try:
                snap = operation.get("snapshot")
                snap = initial if snap == "initial" else snap
                if op == "delete":
                    record["snapshot"] = lake.delete(snap, operation["file"], operation["pos"])
                elif op == "rollback_delete":
                    with lake.catalog.write():
                        lake.delete(snap, operation["file"], operation["pos"])
                        raise RollbackProbe()
                elif op == "prepare":
                    plan = lake.prepare(operation["job"], operation["inputs"], operation["layout"])
                    record["plan"] = plan
                elif op == "publish":
                    if plan is None:
                        raise ValueError("NO_PLAN")
                    record["snapshot"] = lake.publish(plan)
                elif op == "read":
                    record["result"] = lake.read(snap)
                else:
                    raise ValueError("BAD_OPERATION")
            except RollbackProbe:
                record["error"] = "OBSERVER_ROLLBACK"
            except ValueError as exc:
                record["error"] = str(exc)
            record["cost"] = meter.counts()
            record["logical"] = logical(lake, raw)
            result["steps"].append(record)
        return result
    finally:
        raw.close()


def load(path="examples/metadata.json"):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify():
    check()
    pack = load()
    sparse, local = run(pack["sparse"]), run(pack["local"])
    steps = sparse["steps"]
    assert [x["cost"]["write"]["files"] for x in steps[:4]] == [8, 8, 0, 8]
    assert [x["cost"]["write"]["tombstones"] for x in steps[:4]] == [1, 2, 0, 3]
    assert [x["logical"]["head"] for x in steps[:4]] == [2, 3, 3, 3]
    assert steps[3]["error"] == "OBSERVER_ROLLBACK"
    assert steps[3]["logical"] == steps[2]["logical"]
    assert [len(x["result"]["rows"]) for x in steps if x["op"] == "read"] == [16, 15, 14]
    assert steps[-1]["error"] == "BAD_POSITION"
    ls = local["steps"]
    assert ls[2]["cost"]["write"]["files"] == 9
    assert ls[2]["logical"]["dead"] == []
    assert ls[4]["cost"]["write"]["files"] == 8
    assert ls[3]["plan"]["outputs"] == []
    assert [len(x["result"]["rows"]) for x in ls if x["op"] == "read"] == [16, 15, 15, 15]
    assert "rewrite-local-1" in ls[2]["logical"]["files"]
    assert "rewrite-local-1" not in ls[4]["logical"]["files"]
    print("METADATA_OBSERVATION_OK: sparse writes, local replacement, history and rollback")


def main(argv):
    if argv == ["verify"]:
        verify()
    elif argv == ["demo"]:
        for name, spec in load().items():
            print(json.dumps({"case": name, "actual": run(spec)}, ensure_ascii=False))
    elif len(argv) == 2 and argv[0] == "run":
        for name, spec in load(argv[1]).items():
            print(json.dumps({"case": name, "actual": run(spec)}, ensure_ascii=False))
    else:
        raise SystemExit("usage: observe.py verify|demo|run PACK.json")


if __name__ == "__main__":
    main(sys.argv[1:])
