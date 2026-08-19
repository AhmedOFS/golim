"""openterm - Terminal interface for Ollama LLMs"""

__version__ = "0.1.0"
__author__ = "Your Name"
__description__ = "Terminal interface for Ollama LLMs"

# Expose main components for easier imports
from .config import Config

__all__ = [
    "__version__",
    "Config",

]