from __future__ import annotations

from pacl.pending import PendingQueue


def test_enqueue_then_drain_returns_messages_in_order():
    q = PendingQueue()
    q.enqueue("a1", "hello")
    q.enqueue("a1", "world")
    assert q.drain("a1") == ["hello", "world"]


def test_drain_clears_the_queue():
    q = PendingQueue()
    q.enqueue("a1", "hello")
    q.drain("a1")
    assert q.drain("a1") == []


def test_drain_empty_returns_empty_list():
    assert PendingQueue().drain("nobody") == []


def test_enqueue_dedups_exact_duplicate_while_pending():
    q = PendingQueue()
    q.enqueue("a1", "dup")
    q.enqueue("a1", "dup")
    assert q.drain("a1") == ["dup"]


def test_queues_are_per_agent():
    q = PendingQueue()
    q.enqueue("a1", "for-a1")
    q.enqueue("a2", "for-a2")
    assert q.drain("a1") == ["for-a1"]
    assert q.drain("a2") == ["for-a2"]
