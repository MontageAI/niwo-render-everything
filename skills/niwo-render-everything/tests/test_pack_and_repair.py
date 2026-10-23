"""End-to-end external Agent loop: invalid input -> repair -> verified deliverable."""
import contextlib
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pack_bundle
import validate_bundle
from protocol_schema import SCHEMA, validate


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "input"
    (root / "images").mkdir(parents=True)
    (root / "images/a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "content.json").write_text(json.dumps({"schema_version": 1, "title": "测试", "script": "订单列表。"}))
    (root / "manifest.json").write_text(json.dumps({"schema_version": 1, "assets": [
        {"file": "images/a.png", "kind": "image", "summary": "订单列表", "tags": ["订单"]}
    ]}))
    return root


def run(function, args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = function(args)
    return code, json.loads(output.getvalue())


@pytest.mark.parametrize("field,value", [
    ("tags", [str(i) for i in range(9)]), ("tags", ["字" * 33]),
    ("relevance_score", 2), ("relevance_score", float("nan")),
    ("summary", None), ("script_anchor", "字" * 513),
])
def test_errors_are_actionable_then_repair_passes(bundle, field, value):
    path = bundle / "manifest.json"
    manifest = json.loads(path.read_text())
    original = json.dumps(manifest)
    manifest["assets"][0][field] = value
    path.write_text(json.dumps(manifest))
    code, report = run(validate_bundle.main, ["validate_bundle.py", str(bundle), "--json"])
    assert code == 1 and report["status"] == "repair_required"
    assert any(field in e["message"] and "images/a.png" in e["message"] for e in report["errors"])
    path.write_text(original)  # Represents the calling Agent's fix, not silent validator mutation.
    code, report = run(validate_bundle.main, ["validate_bundle.py", str(bundle), "--json"])
    assert code == 0 and report["status"] == "passed"


def test_failed_pack_does_not_publish_then_repair_succeeds(bundle, tmp_path):
    output = tmp_path / "delivery.zip"
    path = bundle / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["assets"][0]["relevance_score"] = 2
    path.write_text(json.dumps(manifest))
    code, report = run(pack_bundle.main, [str(bundle), str(output)])
    assert code == 1 and report["deliverable"] is None
    assert not output.exists()
    manifest["assets"][0]["relevance_score"] = 0.8
    path.write_text(json.dumps(manifest))
    code, report = run(pack_bundle.main, [str(bundle), str(output)])
    assert code == 0 and report["deliverable"] == str(output)
    assert len(report["sha256"]) == 64
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert json.loads(archive.read("manifest.json"))["assets"][0]["relevance_score"] == 0.8


def test_final_zip_recheck_prevents_publish(bundle, tmp_path, monkeypatch):
    actual_check = pack_bundle.check
    calls = []
    def fail_final(directory, options):
        calls.append(directory)
        if len(calls) == 2:
            return {"status": "repair_required", "errors": [{"message": "final check failed"}]}
        return actual_check(directory, options)
    monkeypatch.setattr(pack_bundle, "check", fail_final)
    output = tmp_path / "delivery.zip"
    code, report = run(pack_bundle.main, [str(bundle), str(output)])
    assert code == 1 and len(calls) == 2
    assert report["deliverable"] is None and not output.exists()


def test_optional_metadata_and_url_are_allowed(bundle):
    payload = {name: json.loads((bundle / f"{name}.json").read_text()) for name in ("content", "manifest")}
    payload["content"]["hook_headline"] = None
    assert validate(payload) == []


def test_new_schema_keyword_fails_closed():
    with pytest.raises(ValueError, match="Unsupported schema"):
        validate("test", {"type": "string", "format": "future-format"})


def test_bad_optional_object_reports_missing_fields():
    errors = validate({"prepared_for": "landscape-editorial-motion-v1"},
                      SCHEMA["$defs"]["BundleContent"]["properties"]["editorial"], "content.editorial")
    assert any("title_text" in error for error in errors)
