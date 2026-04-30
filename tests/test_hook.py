"""Tests for hook.py — HookRegistry event system."""

import asyncio

from agenda.hook import HookRegistry


class TestHookRegistry:
    def test_register_and_emit_sync(self) -> None:
        events: list[str] = []
        hooks = HookRegistry()

        def on_test(**kwargs: object) -> None:
            events.append(str(kwargs.get("msg", "")))

        hooks.register("on_test", on_test)
        asyncio.run(hooks.emit("on_test", msg="hello"))
        assert events == ["hello"]

    def test_register_and_emit_async(self) -> None:
        events: list[str] = []
        hooks = HookRegistry()

        async def on_test(**kwargs: object) -> None:
            events.append(str(kwargs.get("msg", "")))

        hooks.register("on_test", on_test)
        asyncio.run(hooks.emit("on_test", msg="world"))
        assert events == ["world"]

    def test_register_multiple_hooks(self) -> None:
        events: list[str] = []
        hooks = HookRegistry()

        def hook_a(**kwargs: object) -> None:
            events.append("a")

        def hook_b(**kwargs: object) -> None:
            events.append("b")

        hooks.register("on_test", hook_a)
        hooks.register("on_test", hook_b)
        asyncio.run(hooks.emit("on_test"))
        assert events == ["a", "b"]

    def test_register_same_hook_multiple_events(self) -> None:
        count: dict[str, int] = {"a": 0, "b": 0}
        hooks = HookRegistry()

        def counter(**kwargs: object) -> None:
            count[str(kwargs.get("event_name", "?"))] += 1

        hooks.register("on_a", counter)
        hooks.register("on_b", counter)

        async def _run() -> None:
            await hooks.emit("on_a", event_name="a")
            await hooks.emit("on_b", event_name="b")

        asyncio.run(_run())
        assert count["a"] == 1
        assert count["b"] == 1

    def test_emit_no_registered_hooks(self) -> None:
        hooks = HookRegistry()
        # Should not raise
        asyncio.run(hooks.emit("nonexistent", foo="bar"))

    def test_remove_specific_hook(self) -> None:
        events: list[str] = []
        hooks = HookRegistry()

        def hook_a(**kwargs: object) -> None:
            events.append("a")

        def hook_b(**kwargs: object) -> None:
            events.append("b")

        hooks.register("on_test", hook_a)
        hooks.register("on_test", hook_b)
        hooks.remove("on_test", hook_a)
        asyncio.run(hooks.emit("on_test"))
        assert events == ["b"]

    def test_remove_all_hooks_for_event(self) -> None:
        events: list[str] = []
        hooks = HookRegistry()

        def hook_a(**kwargs: object) -> None:
            events.append("a")

        hooks.register("on_test", hook_a)
        hooks.remove("on_test")
        asyncio.run(hooks.emit("on_test"))
        assert events == []

    def test_remove_nonexistent_hook(self) -> None:
        hooks = HookRegistry()

        def h(**kwargs: object) -> None:
            pass

        # Should not raise
        hooks.remove("on_test", h)
        hooks.remove("nonexistent")

    def test_hook_exception_does_not_propagate(self) -> None:
        events: list[str] = []
        hooks = HookRegistry()

        def bad_hook(**kwargs: object) -> None:
            raise RuntimeError("boom")

        def good_hook(**kwargs: object) -> None:
            events.append("ok")

        hooks.register("on_test", bad_hook)
        hooks.register("on_test", good_hook)
        asyncio.run(hooks.emit("on_test"))
        assert events == ["ok"]

    def test_hook_exception_async_does_not_propagate(self) -> None:
        events: list[str] = []
        hooks = HookRegistry()

        async def bad_hook(**kwargs: object) -> None:
            raise RuntimeError("async boom")

        def good_hook(**kwargs: object) -> None:
            events.append("ok")

        hooks.register("on_test", bad_hook)
        hooks.register("on_test", good_hook)
        asyncio.run(hooks.emit("on_test"))
        assert events == ["ok"]

    def test_has_returns_correctly(self) -> None:
        hooks = HookRegistry()

        def h(**kwargs: object) -> None:
            pass

        assert not hooks.has("on_test")
        hooks.register("on_test", h)
        assert hooks.has("on_test")
        hooks.remove("on_test")
        assert not hooks.has("on_test")

    def test_remove_hook_not_in_list(self) -> None:
        hooks = HookRegistry()

        def h1(**kwargs: object) -> None:
            pass

        def h2(**kwargs: object) -> None:
            pass

        hooks.register("on_test", h1)
        hooks.remove("on_test", h2)  # Removing unregistered hook on same event
        assert hooks.has("on_test")
        hooks.remove("on_test", h1)
        assert not hooks.has("on_test")

    def test_multiple_kwargs(self) -> None:
        captured: dict[str, object] = {}
        hooks = HookRegistry()

        def on_test(**kwargs: object) -> None:
            captured.update(dict(kwargs))  # type: ignore[arg-type]

        hooks.register("on_test", on_test)
        asyncio.run(hooks.emit("on_test", node_id="n1", config={"a": 1}, iteration=42))
        assert captured["node_id"] == "n1"
        assert captured["config"] == {"a": 1}
        assert captured["iteration"] == 42
