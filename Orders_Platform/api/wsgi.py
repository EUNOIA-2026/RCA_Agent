import logging
import sys

from app import create_app
from config import ConfigError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
)

try:
    app = create_app(start_monitors=True)
except ConfigError as exc:
    logging.getLogger("orders.api").error("CONFIG_ERROR %s", exc)
    sys.exit(1)
