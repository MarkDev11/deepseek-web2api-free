"""Anthropic usage accounting: no more -1 placeholder tokens.

The DSH harness (and any Anthropic-compatible meter) adds usage.input_tokens /
output_tokens to its counters, so a negative sentinel shows up as nonsense in
the token panel. These tests pin real estimates on both the streaming and the
non-streaming builders.
"""
import json

from anthropic_format import (
    _message_delta,
    _message_start,
    build_nonstream_response,
    stream_response,
)


def _parse_sse(chunks):
    events = []
    for chunk in chunks:
        name = None
        data = None
        for line in chunk.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data = line[len("data: "):]
        if name is not None:
            events.append((name, json.loads(data) if data else None))
    return events


class TestMessageStart:
    def test_uses_supplied_input_tokens(self):
        payload = _parse_sse([_message_start("msg_1", "deepseek-chat", input_tokens=42)])
        assert payload[0][0] == "message_start"
        usage = payload[0][1]["message"]["usage"]
        assert usage == {"input_tokens": 42, "output_tokens": 0}

    def test_negative_input_is_clamped(self):
        payload = _parse_sse([_message_start("msg_1", "deepseek-chat", input_tokens=-1)])
        assert payload[0][1]["message"]["usage"]["input_tokens"] == 0


class TestMessageDelta:
    def test_carries_output_tokens(self):
        payload = _parse_sse([_message_delta("end_turn", output_tokens=7)])
        assert payload[0][1]["usage"]["output_tokens"] == 7

    def test_negative_output_is_clamped(self):
        payload = _parse_sse([_message_delta("end_turn", output_tokens=-5)])
        assert payload[0][1]["usage"]["output_tokens"] == 0


class TestNonStreamResponse:
    def test_reports_both_counts(self):
        body = build_nonstream_response(
            "msg_1", "deepseek-chat", content_text="halo dunia", input_tokens=11,
            output_tokens=3,
        )
        assert body["usage"] == {"input_tokens": 11, "output_tokens": 3}

    def test_defaults_are_zero_not_negative(self):
        body = build_nonstream_response("msg_1", "deepseek-chat", content_text="x")
        assert body["usage"] == {"input_tokens": 0, "output_tokens": 0}


class TestStreamUsage:
    def test_stream_reports_positive_counts_and_never_minus_one(self):
        def token_stream():
            yield "halo"
            yield " dunia"
            yield {"__type": "status", "status": "FINISHED"}

        chunks = list(stream_response(
            "msg_1", "deepseek-chat", token_stream(), [], input_tokens=9,
        ))
        events = _parse_sse(chunks)
        starts = [e for e in events if e[0] == "message_start"]
        deltas = [e for e in events if e[0] == "message_delta"]
        assert starts[0][1]["message"]["usage"]["input_tokens"] == 9
        assert deltas[-1][1]["usage"]["output_tokens"] > 0
        assert "-1" not in "".join(chunks)

    def test_thinking_tokens_are_counted(self):
        def token_stream():
            yield {"__type": "thinking", "content": "berpikir dulu"}
            yield "jawaban"

        chunks = list(stream_response(
            "msg_1", "deepseek-chat", token_stream(), [], thinking_mode=True,
        ))
        events = _parse_sse(chunks)
        final = [e for e in events if e[0] == "message_delta"][-1]
        assert final[1]["usage"]["output_tokens"] >= 2
