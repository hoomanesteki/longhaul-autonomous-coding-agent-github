from longlog.chunks import chunk


def test_chunk_keeps_the_last_partial_chunk() -> None:
    assert chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
