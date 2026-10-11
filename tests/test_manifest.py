import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from app import manifest  # noqa: E402

EXAMPLE = """
software_name: YELLOW_METAL_PLATFORM
software_version: 1.0.0
description: yellow metal (jewelry/gold) counting + crowd analytics
installers:
  - name: postgres-database
    image: 192.168.10.21:5000/postgres-database
    image_ver: 2.1.0
    datafile_path: none
    datafile_ver: none
    datafile_md5sum: none
    datafile_size_bytes: none
    scripts_ver: 1.0.0
    scripts_path: /mnt/fileserver4/kumar/scripts/postgres-database_scripts.tar
  - name: jewelry-detection
    image: 192.168.10.21:5000/jewelry-detection
    image_version: 1.0.3
    datafile_path: /mnt/fileserver/RELEASES/YELLOW_METAL/jewelry-detection_1.0.3/models.tar
    datafile_ver: 1.0.0
    build_no: 1
    datafile_md5sum: 2bd165540d9697d6394c6947d988bd5d
    scripts_ver: 1.0.0
    scripts_path: /mnt/fileserver4/kumar/scripts/jewelry-detection_scripts.tar
  - name: stream-ingest
    image: stream-ingest
    image_ver: 1.0.0
    image_source: tar
    image_tar_path: /mnt/fileserver/RELEASES/stream-ingest.tar
    scripts_path: /mnt/fileserver4/kumar/scripts/stream-ingest_scripts.tar
"""


def test_example_parses():
    m, errors, warnings = manifest.parse(EXAMPLE)
    assert not errors, errors
    assert m.software_version == "1.0.0"
    pg, jw, si = m.installers
    assert pg.registry_server == "192.168.10.21:5000" and pg.repo == "postgres-database"
    assert not pg.has_datafile and pg.datafile_size_bytes is None
    assert jw.image_ver == "1.0.3" and jw.has_datafile and jw.datafile_md5sum == "2bd165540d9697d6394c6947d988bd5d"
    assert si.image_source == "tar" and si.registry_server == "" and si.scripts_ver == "1.0.0"
    assert any("scripts_ver missing" in w for w in warnings)


def test_errors():
    bad = EXAMPLE.replace("image_version: 1.0.3", "image_ver: 1.0.3\n    image_version: 1.0.4")
    bad = bad.replace("name: stream-ingest", "name: jewelry-detection")
    bad = bad.replace("192.168.10.21:5000/postgres-database", "192.168.10.21:5000/postgres-database:2.1.0")
    m, errors, _ = manifest.parse(bad)
    assert m is None
    text = "\n".join(errors)
    assert "differ" in text and "duplicate name" in text and "must not contain a tag" in text


def test_registry_source_needs_host():
    m, errors, _ = manifest.parse(EXAMPLE.replace("    image_source: tar\n", ""))
    assert m is None and any("registry host" in e for e in errors)


def test_diff():
    a, _, _ = manifest.parse(EXAMPLE)
    b, _, _ = manifest.parse(EXAMPLE.replace("software_version: 1.0.0", "software_version: 1.0.1")
                             .replace("image_version: 1.0.3", "image_version: 1.0.4"))
    d = manifest.diff(a, b, {}, {})
    assert d["changed"] == ["jewelry-detection"] and set(d["unchanged"]) == {"postgres-database", "stream-ingest"}
