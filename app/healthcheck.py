"""Docker HEALTHCHECK: сервис жив, если heartbeat обновлялся последние 15 минут."""
import os
import sys
import time

data_dir = os.environ.get("DATA_DIR", "/app/data")
try:
    with open(os.path.join(data_dir, "heartbeat")) as f:
        age = time.time() - int(f.read().strip())
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if age < 900 else 1)
