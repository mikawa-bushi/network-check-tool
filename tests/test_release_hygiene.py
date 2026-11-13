"""What a release ships: the license text, and the version every report prints."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_license_is_the_complete_mit_text():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Copyright (c) 2025-2026 hardwork9047" in text
    assert "subject to the following conditions:" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in text
    assert text.rstrip().endswith("OTHER DEALINGS IN THE\nSOFTWARE.")
