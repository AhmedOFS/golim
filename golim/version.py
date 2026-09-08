"""Runtime version accessor; the exact value is generated at build time."""

try:
    from golim._version import __version__
except ImportError:
    __version__ = "0.0.0+unknown"
