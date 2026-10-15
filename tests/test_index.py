from pathlib import Path

from rigveda.chunking import chunks
from rigveda.indexer import ignored, index_paths
from rigveda.search import cite, search
from rigveda.store import connect


def test_index_and_search(tmp_path: Path, config) -> None:
    source = tmp_path / "notes"
    source.mkdir()
    (source / "mission.md").write_text("The Rigveda project runs entirely on local files.")
    stats = index_paths(config.database_path, [str(source)], config)
    assert (stats.updated, stats.unchanged) == (1, 0)
    assert search(config.database_path, "local files", 3)[0]["path"].endswith("mission.md")
    assert index_paths(config.database_path, [str(source)], config).unchanged == 1


def test_deleted_files_are_removed(tmp_path: Path, config) -> None:
    source = tmp_path / "notes"
    source.mkdir()
    (source / "a.txt").write_text("apples")
    (source / "b.txt").write_text("bananas")
    index_paths(config.database_path, [str(source)], config)
    (source / "b.txt").unlink()
    assert index_paths(config.database_path, [str(source)], config).removed == 1
    assert search(config.database_path, "bananas", 3) == []


def test_sibling_folder_with_shared_prefix_is_untouched(tmp_path: Path, config) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs-old").mkdir()
    (tmp_path / "docs-old" / "keep.txt").write_text("archived")
    index_paths(config.database_path, [str(tmp_path / "docs-old")], config)
    assert index_paths(config.database_path, [str(tmp_path / "docs")], config).removed == 0
    assert search(config.database_path, "archived", 3)


def test_hidden_folders_skipped_relative_to_root(tmp_path: Path) -> None:
    root = tmp_path / ".hidden-parent" / "docs"
    assert not ignored(root / "notes.txt", root)
    assert ignored(root / ".git" / "config", root)
    assert ignored(root / "node_modules" / "x.js", root)
    assert ignored(root / "~$report.docx", root)


def test_old_database_is_upgraded(tmp_path: Path) -> None:
    import sqlite3

    db = tmp_path / "old.db"
    old = sqlite3.connect(db)
    old.executescript("""
        CREATE TABLE documents (path TEXT PRIMARY KEY, modified_ns INTEGER NOT NULL, size INTEGER NOT NULL, indexed_at INTEGER NOT NULL);
        CREATE TABLE chunks (id INTEGER PRIMARY KEY, path TEXT NOT NULL, ordinal INTEGER NOT NULL, text TEXT NOT NULL);
    """)
    old.close()
    con = connect(db)
    assert {row["name"] for row in con.execute("PRAGMA table_info(chunks)")} >= {"location"}
    assert db.stat().st_mode & 0o777 == 0o600


def test_cite_formats() -> None:
    assert cite({"path": "/a/talk.mp4", "ordinal": 0, "location": "12:30"}) == "/a/talk.mp4 @ 12:30"
    assert cite({"path": "/a/deck.pptx", "ordinal": 0, "location": "slide 4"}) == "/a/deck.pptx (slide 4)"
    assert cite({"path": "/a/n.txt", "ordinal": 2, "location": ""}) == "/a/n.txt (chunk 3)"


def test_chunks_overlap() -> None:
    assert len(chunks("alpha " * 400, size=100, overlap=20)) > 1


def test_improved_extractor_rereads_only_that_kind(tmp_path: Path, config, monkeypatch) -> None:
    from rigveda import extract

    source = tmp_path / "notes"
    source.mkdir()
    (source / "a.txt").write_text("alpha")
    (source / "b.md").write_text("beta")
    index_paths(config.database_path, [str(source)], config)
    kind, reader, _ = extract.REGISTRY[".txt"]
    monkeypatch.setitem(extract.REGISTRY, ".txt", (kind, reader, "2"))
    stats = index_paths(config.database_path, [str(source)], config)
    assert (stats.updated, stats.unchanged) == (1, 1)
