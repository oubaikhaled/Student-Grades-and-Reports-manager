import os
from whatsapp_automation import run_scheduled_checks_once, init_automation_tables, GowaClient
from database import DatabaseManager

db = DatabaseManager(os.environ["DATABASE_URL"])
init_automation_tables(db)
ok, msg = GowaClient.from_secrets(db).ensure_awake(max_wait_seconds=90)
print(f"GOWA status: {ok} - {msg}")
run_scheduled_checks_once(db)
print("Scheduler tick finished.")
