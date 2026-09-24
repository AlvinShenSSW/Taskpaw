"""Process-wide run generation allocator (#177, core/generation.py)."""

from __future__ import annotations

import threading

from taskpaw_v3.core.generation import next_generation


def test_next_generation_is_monotonic_and_unique_across_threads():
    per_thread = 1000
    results: list[list[int]] = [[] for _ in range(8)]
    start = threading.Event()

    def work(bucket: list[int]) -> None:
        start.wait(5)
        for _ in range(per_thread):
            bucket.append(next_generation())

    threads = [
        threading.Thread(target=work, args=(b,), name=f"gen-{i}", daemon=True)
        for i, b in enumerate(results)
    ]
    for t in threads:
        t.start()
    start.set()
    for t in threads:
        t.join(10)
        assert not t.is_alive()
    everything = [g for bucket in results for g in bucket]
    assert len(everything) == 8 * per_thread
    assert len(set(everything)) == len(everything)  # never reused
    for bucket in results:  # each caller sees a strictly increasing sequence
        assert bucket == sorted(bucket) and len(set(bucket)) == len(bucket)
    assert all(g >= 1 for g in everything)


def test_next_generation_keeps_increasing():
    a = next_generation()
    b = next_generation()
    assert b == a + 1
