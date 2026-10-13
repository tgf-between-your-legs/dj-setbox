from rekordbox_mcp.engine import llm


class Fake:
    def __init__(self):
        self.messages = self; self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        items = [{"id": "1", "title": "Clean", "artist": "X"}]
        return type("M", (), {"content": [type("B", (), {"type": "tool_use", "input": {"tracks": items}})()]})()


def test_routing_defaults():
    assert llm.model_for("bulk") == "claude-haiku-5-5"
    assert llm.model_for("structure") == "claude-sonnet-5-5"
    assert llm.model_for("critique") == "claude-opus-5-5"


def test_clean_names_chunks_on_haiku():
    f = Fake()
    rows = llm.clean_names([{"id": str(i), "title": "t", "artist": "a"} for i in range(320)], client_=f)
    assert len(f.calls) == 3 and all(c["model"] == "claude-haiku-5-5" for c in f.calls)
    assert rows[0]["title"] == "Clean"


def test_tool_call_uses_auto_choice_and_retries_on_text():
    """Live API (2026-10-13): forced tool_choice is rejected by current models; we use auto + one retry."""
    class TwoStep:
        def __init__(self):
            self.messages = self; self.calls = []

        def create(self, **kw):
            self.calls.append(kw)
            if len(self.calls) == 1:
                return type("M", (), {"content": [type("T", (), {"type": "text", "text": "sure"})()]})()
            return type("M", (), {"content": [type("B", (), {"type": "tool_use", "name": "x", "input": {"ok": 1}})()]})()
    c = TwoStep()
    assert llm.tool_call("s", "u", "x", {"type": "object"}, client_=c) == {"ok": 1}
    assert len(c.calls) == 2 and all(k["tool_choice"] == {"type": "auto"} for k in c.calls)
