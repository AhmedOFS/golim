import logging

LOG_FORMAT = '%(levelname)s: %(message)s'


def setup_root_logger(debug: bool = False) -> None:
    level = logging.DEBUG if debug else logging.WARNING
    logging.basicConfig(level=level, format=LOG_FORMAT, force=True)
