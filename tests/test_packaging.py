"""Build helpers: the RIFE download is checked and cannot write outside its folder."""
import hashlib
import importlib.util
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _fetch_rife():
    spec = importlib.util.spec_from_file_location("fetch_rife", ROOT / "packaging" / "fetch_rife.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _zip(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(mod, monkeypatch, zpath, dest, sha):
    monkeypatch.setitem(mod.SHA256, "ubuntu", sha)
    monkeypatch.setattr(sys, "argv", ["fetch_rife.py", "ubuntu", "--zip", str(zpath), "--dest", str(dest)])
    return mod.main()


def test_only_the_needed_files_are_extracted(tmp_path, monkeypatch):
    mod = _fetch_rife()
    zpath = tmp_path / "rife.zip"
    sha = _zip(zpath, {"rife-x/rife-ncnn-vulkan": b"exe", "rife-x/LICENSE": b"MIT",
                       "rife-x/rife-v4.6/flownet.param": b"p", "rife-x/rife-v2/flownet.param": b"old"})
    dest = tmp_path / "out"
    assert _run(mod, monkeypatch, zpath, dest, sha) == 0
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()) == [
        "LICENSE", "rife-ncnn-vulkan", "rife-v4.6/flownet.param"]


def test_a_path_that_climbs_out_of_the_folder_is_refused(tmp_path, monkeypatch):
    mod = _fetch_rife()
    zpath = tmp_path / "evil.zip"
    sha = _zip(zpath, {"rife-x/rife-ncnn-vulkan": b"exe",
                       "rife-x/rife-v4.6/../../../escaped.txt": b"owned"})
    dest = tmp_path / "a" / "b" / "out"
    assert _run(mod, monkeypatch, zpath, dest, sha) == 1
    assert not list(tmp_path.rglob("escaped.txt"))


def test_a_wrong_checksum_is_refused(tmp_path, monkeypatch):
    mod = _fetch_rife()
    zpath = tmp_path / "rife.zip"
    _zip(zpath, {"rife-x/rife-ncnn-vulkan": b"exe"})
    assert _run(mod, monkeypatch, zpath, tmp_path / "out", "0" * 64) == 1
    assert not (tmp_path / "out" / "rife-ncnn-vulkan").exists()
