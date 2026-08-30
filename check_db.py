import sqlite3, os
p = "webhook_events.db"
print("exists:", os.path.exists(p), "size:", os.path.getsize(p) if os.path.exists(p) else 0)
c = sqlite3.connect(p)
tables = [r[0] for r in c.execute("select name from sqlite_master where type='table'")]
print("tables:", tables)
for t in tables:
    print(t, c.execute(f"select count(*) from {t}").fetchone()[0])
