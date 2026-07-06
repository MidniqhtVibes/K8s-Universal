import os
import tempfile
from pathlib import Path

TEST_ROOT = Path(tempfile.mkdtemp(prefix="cluster-builder-tests-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{TEST_ROOT / 'test.db'}")
os.environ.setdefault("MASTER_KEY", "test-master-key-that-is-never-used-in-production")
os.environ.setdefault("SESSION_SECRET", "test-session-secret-that-is-never-used-in-production")
os.environ.setdefault("INITIAL_ADMIN_PASSWORD", "test-admin-password")
os.environ.setdefault("DATA_ROOT", str(TEST_ROOT / "data"))
os.environ.setdefault("SOURCE_ROOT", str(Path(__file__).parents[1]))

