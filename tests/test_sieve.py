"""Unit tests for tool_sieve.StreamSieve.

Run with:  python -m pytest tests/test_sieve.py -v
"""
import pytest

from tool_sieve import StreamSieve, SieveEvent


def _collect_text(sieve, chunks):
    out = []
    for ch in chunks:
        for evt in sieve.feed(ch):
            if evt.type == "text":
                out.append(evt.data)
    out.extend(d.data for d in sieve.flush() if d.type == "text")
    return "".join(out)


def _parse_fn_stub(text):
    """A trivial parse function: any text containing a closing </tool_calls>
    pair produces a single dummy tool call.
    """
    if "<tool_calls>" in text and "</tool_calls>" in text:
        return ([{"id": "x", "function": {"name": "f", "arguments": "{}"}}], "")
    return ([], text)


def test_plain_text_passes_through():
    s = StreamSieve(parse_fn=_parse_fn_stub)
    out = _collect_text(s, ["hello", " ", "world"])
    assert out == "hello world"


def test_tool_call_detected_and_split_from_text():
    s = StreamSieve(parse_fn=_parse_fn_stub)
    out = _collect_text(s, [
        "pre ", "<tool_calls>", "<invoke name=\"f\" />", "</tool_calls>", " post"
    ])
    assert "pre " in out
    assert " post" in out
    assert "<tool_calls>" not in out


def test_tail_hold_for_potential_tag():
    """A bare `<` must be held back, not flushed as text — it could grow
    into a tool call tag in a later chunk.
    """
    s = StreamSieve(parse_fn=_parse_fn_stub)
    # Build a stream where text is interrupted by a bare '<' and then
    # some more characters arrive that are clearly NOT a tag (a newline,
    # for example). The sieve should keep holding the prefix but the
    # '<' cannot start a DSML tag once followed by a newline.
    s.feed("a")  # "a" emitted
    s.feed("b<")  # "b" emitted (held = "<")
    events = s.feed("\nmore")
    # The '<' is no longer a tag prefix once we see the newline; the
    # buffered "<" should now be emitted as text along with "\nmore".
    text_so_far = "".join(e.data for e in events if e.type == "text")
    assert "<" in text_so_far
    assert "\n" in text_so_far or "more" in text_so_far


def test_recursive_feed_does_not_stack_overflow():
    """Many tool calls back-to-back must not trigger Python recursion limits
    in the iterative drain path.
    """
    s = StreamSieve(parse_fn=_parse_fn_stub)
    chunks = []
    for i in range(50):
        chunks.append(f"t{i} ")
        chunks.append("<tool_calls>")
        chunks.append("<invoke name=\"f\" />")
        chunks.append("</tool_calls>")
    out = _collect_text(s, chunks)
    # All the inter-call text should appear, but no tool-call markers.
    for i in range(50):
        assert f"t{i} " in out
    assert "<tool_calls>" not in out
    assert "</tool_calls>" not in out


def test_capture_buffer_size_limit():
    """An adversarial stream of more than 1 MiB of `<` characters should
    be force-flushed as text and not deadlock the sieve.
    """
    s = StreamSieve(parse_fn=_parse_fn_stub, max_capture_buffer=128)
    big = "<" * 200
    out = _collect_text(s, [big, "more"])
    # Nothing matches; everything should be emitted as text.
    assert big in out
    assert "more" in out


def test_empty_input_safe():
    s = StreamSieve(parse_fn=_parse_fn_stub)
    assert s.feed("") == []
    assert s.flush() == []


def test_split_safe_empty_string():
    s = StreamSieve(parse_fn=_parse_fn_stub)
    # Edge case: chunk is empty / whitespace.
    out = _collect_text(s, [""])
    assert out == ""


def _collect(sieve, chunks):
    events = [e for ch in chunks for e in sieve.feed(ch)] + sieve.flush()
    text = "".join(e.data for e in events if e.type == "text")
    calls = [c for e in events if e.type == "tool_calls" for c in e.data]
    return text, calls


def test_tokenized_dsml_tool_call_leaks_no_text():
    # DeepSeek streams tags in small pieces; "<" followed by "|" used to
    # emit the "<" as text (and then leak the closing tag).
    from tool_dsml import parse_dsml_tool_calls
    s = StreamSieve(parse_fn=lambda t: parse_dsml_tool_calls(t, ["get_weather"]))
    chunks = ["<", "|", "DSML", "|", "tool", "_calls", ">\n",
              "<", "|", "DSML", "|", 'invoke name="get_weather">',
              '<|DSML|parameter name="city" string="true">Berlin</|DSML|parameter>',
              "</|DSML|invoke>\n", "</", "|DSML|", "tool_calls>"]
    text, calls = _collect(s, chunks)
    assert text == ""
    assert len(calls) == 1


def test_lone_lt_and_pipe_in_plain_text_pass_through():
    s = StreamSieve(parse_fn=_parse_fn_stub)
    assert _collect_text(s, ["a < b", " | c"]) == "a < b | c"
