from __future__ import annotations

from app.index.v3.layer_codec import PostingLayerReader

from .test_reader import ReaderCorpus, _open_incremental, reader_corpus


def test_stale_rows_are_filtered_before_search_posting_allocation(
    reader_corpus: ReaderCorpus,
    monkeypatch,
) -> None:
    with _open_incremental(reader_corpus) as reader:
        unfiltered = 0
        for layer in reader._layers_newest:
            record = layer.reader.lookup_term("titlehot")
            if record is not None and record.has_postings:
                unfiltered += sum(1 for _ in layer.reader._iter_complete(record))

        original = PostingLayerReader._search_posting
        constructed = 0

        def counting_search_posting(self, *args, **kwargs):
            nonlocal constructed
            constructed += 1
            return original(self, *args, **kwargs)

        monkeypatch.setattr(
            PostingLayerReader,
            "_search_posting",
            counting_search_posting,
        )

        rows = list(reader.iter_raw_postings("titlehot"))

    assert unfiltered > len(rows)
    assert constructed == len(rows)
