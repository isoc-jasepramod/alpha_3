import sys
import os

project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from datetime import datetime
import uvicorn
import yaml
from loguru import logger


def main():
    config_path = os.path.join(os.path.dirname(__file__), "..", "config", "settings.yaml")
    host = "0.0.0.0"
    port = 8000
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            cfg = yaml.safe_load(f).get("server", {})
            host = cfg.get("host", host)
            port = int(cfg.get("port", port))

    # Configure persistent rotating file logging
    today_str = datetime.now().strftime("%Y-%m-%d")
    log_dir = os.path.join(project_root, "logs", today_str)
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "app.log")
    logger.add(log_file, rotation="50 MB", retention="7 days", level="DEBUG", enqueue=True)

    logger.info(f"🚀 Starting Project Alpha 2.0 Backend on http://{host}:{port}")
    logger.info(f"📁 Logging output mirrored to file: {log_file}")
    uvicorn.run(
        "backend.api.server:app",
        host=host,
        port=port,
        reload=False,
        log_level="info"
    )

if __name__ == "__main__":
    main()
