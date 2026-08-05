import atexit
import os
import tempfile


_test_db_path = os.path.join(tempfile.gettempdir(), f"hivedeploy-tests-{os.getpid()}.db")
os.environ.setdefault("DB_PATH", _test_db_path)


@atexit.register
def _remove_test_database():
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(_test_db_path + suffix)
        except FileNotFoundError:
            pass
