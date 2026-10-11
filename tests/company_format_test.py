"""Company-format scripts tar (compose_template.yml + pre_start.sh): manager scan + agent prestart/render.

  cd /mnt/c/software-manager && python3 tests/company_format_test.py
"""
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX = HERE / "fixtures" / "company_scripts"


def make_tar(dest: Path, names: dict[str, str]) -> Path:
    with tarfile.open(dest, "w") as tf:
        for arc, src in names.items():
            data = (FIX / src).read_bytes().replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(arc)
            info.size, info.mode = len(data), 0o755
            tf.addfile(info, io.BytesIO(data))
    return dest


def main() -> int:
    sys.path.insert(0, str(HERE.parent / "src"))
    from app import releases  # noqa: E402

    td = Path(tempfile.mkdtemp())
    ok = make_tar(td / "crowding-controller_scripts.tar",
                  {"compose_template.yml": "compose_template.yml", "pre_start.sh": "pre_start.sh"})
    info = releases.check_scripts_tar(ok)
    print("scan company tar: OK, warnings:", info["warnings"])

    bad = make_tar(td / "bad.tar", {"compose_template.yml": "compose_template.yml"})
    try:
        releases.check_scripts_tar(bad)
        print("FAIL: tar without pre_start.sh passed")
        return 1
    except releases.ScanError as exc:
        print("scan tar without pre_start.sh: rejected:", exc)

    data = td / "pod"
    data.mkdir()
    p = subprocess.run(["bash", str(FIX / "pre_start.sh"), "crowding-controller"], cwd=data,
                       capture_output=True, text=True)
    env_file = data / "crowding-controller.env"
    assert p.returncode == 0 and env_file.is_file() and (data / "serviceslog/crowding_controller").is_dir(), p.stderr
    print("pre_start.sh with cwd=data dir and $1=container name:", p.stdout.strip())
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
