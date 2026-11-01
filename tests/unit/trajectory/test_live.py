"""TrajectoryLiveBus — in-process pub/sub per thread_id."""

from __future__ import annotations

from octop.infra.history.trajectory.live import TrajectoryLiveBus


def test_publish_delivers_to_subscriber() -> None:
    bus = TrajectoryLiveBus()
    queue = bus.subscribe("T1")
    message = {"event_id": "e1", "kind": "user"}

    bus.publish("T1", message)

    assert queue.get_nowait() == message


def test_unsubscribe_stops_delivery_and_isolates_threads() -> None:
    bus = TrajectoryLiveBus()
    t1 = bus.subscribe("T1")
    t2 = bus.subscribe("T2")

    bus.unsubscribe("T1", t1)
    bus.publish("T1", {"event_id": "dropped"})
    bus.publish("T2", {"event_id": "kept"})

    assert t1.empty()
    assert t2.get_nowait() == {"event_id": "kept"}


def test_slow_subscriber_keeps_only_latest_bounded_events() -> None:
    bus = TrajectoryLiveBus(subscriber_queue_size=2)
    queue = bus.subscribe("T1")

    for seq in range(4):
        bus.publish("T1", {"seq": seq})

    assert [queue.get_nowait(), queue.get_nowait()] == [{"seq": 2}, {"seq": 3}]


def test_filtered_subscriber_is_not_evicted_by_parent_tokens_and_reports_native_overflow() -> None:
    bus = TrajectoryLiveBus(subscriber_queue_size=2)
    queue = bus.subscribe("T", kinds=["subagent"])
    bus.publish("T", {"kind": "subagent", "seq": 1})
    for _ in range(500):
        bus.publish("T", {"kind": "assistant"})
    assert queue.qsize() == 1
    assert not bus.overflowed(queue)
    bus.publish("T", {"kind": "subagent", "seq": 2})
    bus.publish("T", {"kind": "subagent", "seq": 3})
    assert bus.overflowed(queue)
    bus.unsubscribe("T", queue)
    assert not bus.overflowed(queue)
