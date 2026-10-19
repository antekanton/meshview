import sys
import tempfile

# meshview.config reads --config at import time, so point it at a throwaway file
# before any meshview module is imported.
with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as _config:
    _config.write(
        "[database]\n"
        "connection_string = sqlite+aiosqlite:///:memory:\n"
        "[server]\n"
        "bind = 127.0.0.1\n"
        "port = 8081\n"
        "tls_cert =\n"
    )
sys.argv = [sys.argv[0], "--config", _config.name]
