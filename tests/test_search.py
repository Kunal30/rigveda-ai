from pathlib import Path

from rigveda import llm
from rigveda.indexer import index_paths
from rigveda.search import search
from rigveda.store import connect


def _quiet(message: str) -> None:
    pass


def test_meaning_search_finds_paraphrase(tmp_path: Path, config) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "lease.txt").write_text("You will pay $ 2605.00 per month for rent, payable in advance.")
    for i in range(5):
        (docs / f"addendum{i}.txt").write_text(f"Addendum {i}. Garage, carport and storage permits, remotes and parking stickers add to monthly rent.")
    index_paths(config.database_path, [str(docs)], config, log=_quiet)
    keyword_only = search(config.database_path, "What is my monthly rent?", 3)
    hybrid = search(config.database_path, "What is my monthly rent?", 3, config)
    assert not any("2605" in row["text"] for row in keyword_only)
    assert any("2605" in row["text"] for row in hybrid)


def test_duplicate_copies_appear_once(tmp_path: Path, config) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in ("Lease.txt", "Lease copy.txt"):
        (docs / name).write_text("Security deposit is $ 150.00")
    (docs / "other.txt").write_text("The deposit box is in the lobby")
    index_paths(config.database_path, [str(docs)], config, log=_quiet)
    texts = [row["text"] for row in search(config.database_path, "security deposit", 5, config)]
    assert texts.count("Security deposit is $ 150.00") == 1 and len(texts) == 2


def test_embeddings_removed_with_their_file(tmp_path: Path, config) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("alpha")
    index_paths(config.database_path, [str(docs)], config, log=_quiet)
    (docs / "a.txt").unlink()
    index_paths(config.database_path, [str(docs)], config, log=_quiet)
    assert connect(config.database_path).execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0


def test_keywords_still_work_when_ollama_is_down(tmp_path: Path, config, monkeypatch) -> None:
    def down(*args):
        raise llm.OllamaError("connection refused")

    monkeypatch.setattr(llm, "embed", down)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.txt").write_text("visa appointment")
    index_paths(config.database_path, [str(docs)], config, log=_quiet)
    assert search(config.database_path, "visa", 3, config)[0]["text"] == "visa appointment"
    # Once Ollama is back, the missing vectors are filled in on the next index.
    from tests.conftest import fake_embed

    monkeypatch.setattr(llm, "embed", fake_embed)
    index_paths(config.database_path, [str(docs)], config, log=_quiet)
    assert connect(config.database_path).execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 1


def test_new_chunks_are_found_without_reloading_and_removed_ones_dropped(tmp_path: Path, config) -> None:
    from rigveda import embeddings

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("passport renewal appointment")
    index_paths(config.database_path, [str(docs)], config, log=_quiet)
    assert search(config.database_path, "passport", 1, config)[0]["text"] == "passport renewal appointment"
    vectors = next(iter(embeddings._cache.values())) if len(embeddings._cache) == 1 else None
    (docs / "b.txt").write_text("electricity bill paid in March")
    index_paths(config.database_path, [str(docs)], config, log=_quiet)
    assert search(config.database_path, "electricity bill", 1, config)[0]["text"] == "electricity bill paid in March"
    (docs / "a.txt").unlink()
    index_paths(config.database_path, [str(docs)], config, log=_quiet)
    assert all("passport" not in row["text"] for row in search(config.database_path, "passport", 3, config))
    if vectors is not None:
        assert vectors.matrix.dtype.name == "float32" and len(vectors.matrix) == len(vectors.ids) == 1


def test_meaning_search_uses_the_server_and_falls_back_when_it_is_down(tmp_path: Path, config, monkeypatch) -> None:
    from rigveda import embeddings

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.txt").write_text("visa interview at the consulate")
    index_paths(config.database_path, [str(docs)], config, log=_quiet)
    monkeypatch.setattr(embeddings, "SERVER", "http://127.0.0.1:9")  # nothing listens there
    assert search(config.database_path, "consulate visa", 1, config)[0]["text"] == "visa interview at the consulate"
    asked = []
    monkeypatch.setattr(embeddings, "_from_server", lambda question, limit, database: asked.append(question) or [])
    search(config.database_path, "consulate visa", 1, config)
    assert asked == ["consulate visa"]
