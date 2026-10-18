import os
# adjust these two imports to your real file/class names
from whatsapp_automation import run_scheduled_checks_once, GowaClient
from database import Database

db = Database(os.environ["DATABASE_URL"])  # build it the same way your app does

# wake the Render GOWA instance first, since it hibernates
GowaClient.from_secrets(db).ensure_awake(max_wait_seconds=90)

run_scheduled_checks_once(db)
