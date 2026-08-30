import sqlite3, json
c = sqlite3.connect("webhook_events.db")
for t in ("webhook_events", "events", "audit"):
    cur = c.execute(f"select * from {t} order by rowid desc limit 2")
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    print(f"--- {t} ({len(rows)} shown) cols={cols}")
    for r in rows:
        print(json.dumps(dict(zip(cols, [str(x)[:300] for x in r])), indent=2))
