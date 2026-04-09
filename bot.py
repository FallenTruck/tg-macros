import logging
import warnings

warnings.filterwarnings(
    "ignore",
    message="urllib3 v2 only supports OpenSSL 1.1.1+.*",
)

import urllib3

from macro_bot.telegram_app import build_telegram_application

urllib3.disable_warnings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def main() -> None:
    app = build_telegram_application()
    app.run_polling()


if __name__ == "__main__":
    main()
