from fs_diloco.atomic_io import atomic_write_json, read_json


def test_atomic_json_ignores_tmp_until_final(tmp_path):
    target = tmp_path / "control" / "latest.json"
    atomic_write_json(target, {"version": 1})
    assert read_json(target) == {"version": 1}
    assert not list(target.parent.glob("*.tmp"))
