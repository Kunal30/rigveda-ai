from pathlib import Path

from rigveda.cli import chunks, index, search


def test_index_and_search(tmp_path: Path) -> None:
    source = tmp_path / "notes"
    source.mkdir()
    (source / "mission.md").write_text("The Rigveda project runs entirely on local files.")
    database = tmp_path / "index.db"
    assert index(database, [str(source)]) == (1, 0)
    assert search(database, "local files", 3)[0]["path"].endswith("mission.md")


def test_chunks_overlap() -> None:
    assert len(chunks("alpha " * 400, size=100, overlap=20)) > 1
