#!/bin/bash

prepare_actor_identity() {
  local descriptor="$1"
  local project_root="$2"
  local python_bin="$3"
  local -a descriptor_fields actual_source

  readarray -t descriptor_fields < <(
    "$python_bin" - "$descriptor" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    descriptor = json.load(handle)
print(descriptor["resolved_config_path"])
print(descriptor["descriptor_sha256"])
print(descriptor["git_commit"])
print(descriptor["source_fingerprint"])
print(int(bool(descriptor["git_dirty"])))
print(descriptor["mode"])
PY
  )
  FS_DILOCO_RESOLVED_CONFIG="${descriptor_fields[0]}"
  FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256="${descriptor_fields[1]}"
  FS_DILOCO_EXPECTED_GIT_COMMIT="${descriptor_fields[2]}"
  FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT="${descriptor_fields[3]}"
  FS_DILOCO_EXPECTED_GIT_DIRTY="${descriptor_fields[4]}"
  FS_DILOCO_MEMBERSHIP_MODE="${descriptor_fields[5]}"

  readarray -t actual_source < <(
    "$python_bin" - "$project_root" <<'PY'
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(sys.argv[1])))
from fs_diloco.core.source_identity import capture_source_identity

identity = capture_source_identity(pathlib.Path(sys.argv[1]))
print(identity["git_commit"])
print(identity["source_fingerprint"])
print(int(identity["git_dirty"]))
PY
  )
  test "${actual_source[0]}" = "$FS_DILOCO_EXPECTED_GIT_COMMIT"
  test "${actual_source[1]}" = "$FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT"
  test "${actual_source[2]}" = "$FS_DILOCO_EXPECTED_GIT_DIRTY"

  FS_DILOCO_GIT_COMMIT="$FS_DILOCO_EXPECTED_GIT_COMMIT"
  FS_DILOCO_SOURCE_FINGERPRINT="$FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT"
  FS_DILOCO_GIT_DIRTY="$FS_DILOCO_EXPECTED_GIT_DIRTY"
  FS_DILOCO_REQUIRE_SOURCE_IDENTITY=1
  export FS_DILOCO_RESOLVED_CONFIG FS_DILOCO_MEMBERSHIP_MODE
  export FS_DILOCO_EXPECTED_DESCRIPTOR_SHA256 FS_DILOCO_EXPECTED_GIT_COMMIT
  export FS_DILOCO_EXPECTED_SOURCE_FINGERPRINT FS_DILOCO_EXPECTED_GIT_DIRTY
  export FS_DILOCO_GIT_COMMIT FS_DILOCO_SOURCE_FINGERPRINT FS_DILOCO_GIT_DIRTY
  export FS_DILOCO_REQUIRE_SOURCE_IDENTITY
}
