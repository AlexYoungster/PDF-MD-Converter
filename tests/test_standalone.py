"""Installed entry points and the README's complete portable demo flow."""
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys


def test_installed_package_and_help_outside_checkout(tmp_path):
    package = importlib.metadata.distribution("pdf-md-converter")
    entries = {entry.name for entry in package.entry_points if entry.group == "console_scripts"}
    assert {"pdf-md-converter", "doc2md", "doc2md-check"} <= entries
    assert not any("condition-extractor" in requirement.lower() for requirement in package.requires or [])
    isolated = subprocess.run(
        [sys.executable, "-I", "-c", "import document_converter, sys; "
         "assert not any(m == 'condition_extractor' or m.startswith('condition_extractor.') for m in sys.modules)"],
        cwd=tmp_path, capture_output=True,
    )
    assert isolated.returncode == 0, isolated.stderr
    for module in ("document_converter", "document_converter.fidelity"):
        result = subprocess.run([sys.executable, "-X", "utf8", "-m", module, "--help"], cwd=tmp_path,
                                capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr
        assert "--output" in result.stdout


def test_original_demo_cli_conversion_and_check(tmp_path):
    builder = Path(__file__).resolve().parents[1] / "examples/create_demo.py"
    source, md, check = tmp_path / "demo.pdf", tmp_path / "demo.md", tmp_path / "check"
    commands = [
        [sys.executable, "-X", "utf8", str(builder), str(source)],
        [sys.executable, "-X", "utf8", "-m", "document_converter", str(source), "-o", str(md)],
        [sys.executable, "-X", "utf8", "-m", "document_converter.fidelity", str(source), str(md),
         "-o", str(check), "--ocr", "none"],
    ]
    for cmd in commands:
        result = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8")
        assert result.returncode == 0, result.stderr + result.stdout
    audit = json.loads(md.with_suffix(".audit.json").read_text(encoding="utf-8"))
    assert audit["pdf_pages"] == 3 and audit["tables"] == 1
    assert len(audit["volumes"]) == 2
    text = md.read_text(encoding="utf-8")
    assert "BOOKMARK_ONLY_NOT_EXTRACTED" not in text
    assert 'rowspan="2"' in text and 'colspan="2"' in text
    assert "numeric-fraction" in text and "script-run" in text
    assert "6.102x10" in text and "6.102×10" not in text
    summary = json.loads((check / "summary.json").read_text(encoding="utf-8"))
    assert summary["complete_text_layer_check"]
    assert not summary["severity_counts"].get("error")
