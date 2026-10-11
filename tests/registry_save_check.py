"""Check registry.image_size / save_image against a manager state dir (registry creds saved in Settings).

  SM_STATE_DIR=<state> python registry_save_check.py <host> <repo> <tag> <dest.tar>
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from app import registry  # noqa: E402

host, repo, tag, dest = sys.argv[1:5]
t = time.time()
print("size", registry.image_size(host, repo, tag, "linux/amd64"), f"{time.time() - t:.1f}s", flush=True)
t = time.time()
print("digest", registry.save_image(host, repo, tag, f"{host}/{repo}:{tag}", Path(dest), "linux/amd64"),
      f"{time.time() - t:.1f}s", Path(dest).stat().st_size)
