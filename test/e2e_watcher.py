# scripts/e2e_watcher.py — throwaway dev script, not shipped
#
# 1. import your CSVHandler from app.ingest.handler
# 2. import Observer from watchdog.observers
# 3. instantiate the handler and the observer
# 4. observer.schedule(handler, path="data/inbox", recursive=False)
# 5. observer.start()
# 6. try:    while True: time.sleep(1)
#    except KeyboardInterrupt:  observer.stop()
# 7. observer.join()

from app.ingest.handler import CSVHandler
from watchdog.observers import Observer

import os

from dotenv import load_dotenv

load_dotenv()


import time


if __name__ == "__main__":
    INBOX_PATH = os.getenv("INBOX")
    event_handler = CSVHandler()
    observer = Observer()

    observer.schedule(event_handler, INBOX_PATH, recursive=True)
    observer.start()
    try:
        while True: 
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()



