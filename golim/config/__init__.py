"""Configuration package for golim."""
from .config import Config, ConfigSchemaError, get_config, init_config

__all__ = ["Config", "ConfigSchemaError", "get_config", "init_config"]
