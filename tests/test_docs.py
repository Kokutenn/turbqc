"""Generated documentation stays in sync with the code and every requirement is covered."""
from turbqc import config, docs


def test_docs_generate_and_cover_every_requirement(tmp_path, monkeypatch):
    monkeypatch.setattr(docs, "DOCS", tmp_path)
    paths = docs.generate()
    assert {p.name for p in paths} == {"rule_catalogue.md", "data_dictionary.md", "traceability.md"}
    trace = (tmp_path / "traceability.md").read_text(encoding="utf-8")
    assert "| NO |" not in trace
    catalogue = (tmp_path / "rule_catalogue.md").read_text(encoding="utf-8")
    assert all(r["id"] in catalogue for r in config.rules())
    assert "${" not in catalogue, "unrendered placeholder in catalogue"
