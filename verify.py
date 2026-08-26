import sqlite3

conn = sqlite3.connect("webhook_events.db")

print("--- tamper test ---")
conn.execute(
    "insert into audit (payment_id, event_id, action, created_at) "
    "values (NULL, 'evt_probe', 'ingested', '2026-01-01T00:00:00Z')"
)
print("probe row inserted (INSERT must stay open — audit is append-only, not read-only)")

for sql in ("update audit set action='x' where event_id='evt_probe'",
            "delete from audit where event_id='evt_probe'"):
    try:
        conn.execute(sql)
        print(f"!! SUCCEEDED (not protected): {sql}")
    except sqlite3.IntegrityError as e:
        print(f"blocked: {e}")
    except sqlite3.Error as e:
        print(f"!! wrong error ({type(e).__name__}): {e}")

conn.rollback()
conn.close()