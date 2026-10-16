"""Malformed packet properties against the production parsers."""

import pytest
from hypothesis import example, given, strategies as st

from superlink import appmsg
from superlink.adopt import decode_adopt_response
from superlink.decoder import parse_frame


@given(st.binary(max_size=512))
def test_frame_parser_preserves_bytes_or_rejects_short_input(raw):
    frame = parse_frame(raw)
    if len(raw) < 14:
        assert frame is None
    else:
        assert frame is not None
        assert bytes([frame.mctrl, frame.dctrl]) + frame.mac + bytes([
            frame.seq_hi, frame.seq_lo]) + frame.encrypted == raw
        assert len(frame.mac) == 6
        assert len(frame.encrypted) >= 4
        assert frame.payload is None


@given(body=st.binary(max_size=512),
       sizes=st.dictionaries(st.integers(0, 255), st.integers(0, 32), max_size=20))
@example(body=b"\x0a\x00" + bytes(29), sizes={})
@example(body=b"\x0a\x00" + bytes(29) + b"\xff", sizes={})
def test_application_parser_returns_a_message_or_value_error(body, sizes):
    try:
        decoded = appmsg.decode_message(body, sizes=sizes)
    except ValueError:
        return
    assert decoded["messageId"] == body[0]
    assert decoded["messageTag"] == body[1]


@st.composite
def device_info_messages(draw):
    tag = draw(st.integers(0, 255))
    fixed = draw(st.binary(min_size=29, max_size=29))
    messages = draw(st.binary(max_size=20))
    properties = draw(st.lists(st.tuples(*(st.integers(0, 255) for _ in range(3))),
                               max_size=20))
    body = bytes([10, tag]) + fixed + bytes([len(messages)]) + messages
    body += bytes([len(properties)]) + bytes(v for prop in properties for v in prop)
    return body, messages, properties


@given(device_info_messages(), st.data())
def test_device_info_truncation_is_rejected_without_partial_metadata(message, data):
    body, messages, properties = message
    decoded = appmsg.decode_message(body)
    assert decoded["supportedMessageIds"] == list(messages)
    assert decoded["supportedProperties"] == [
        {"propertyId": p, "channelCount": c, "valueSize": n} for p, c, n in properties
    ]
    cut = data.draw(st.integers(0, len(body) - 1), label="truncation offset")
    with pytest.raises(ValueError):
        appmsg.decode_message(body[:cut])


@given(pid=st.integers(0, 255), channel=st.integers(0, 255),
       value=st.binary(min_size=1, max_size=64), data=st.data(), dynamic=st.booleans())
def test_truncated_known_property_is_not_reported_as_a_complete_value(
        pid, channel, value, data, dynamic):
    cut = data.draw(st.integers(0, len(value) - 1), label="value truncation")
    header = bytes([12, 0, pid, channel])
    prefix = bytes([len(value)]) if dynamic else b""
    sizes = {pid: 0 if dynamic else len(value)}
    complete = appmsg.decode_message(header + prefix + value, sizes=sizes)
    assert complete["properties"][0]["value"] == value
    partial = appmsg.decode_message(header + prefix + value[:cut], sizes=sizes)
    assert partial["properties"] == []


@given(st.binary(min_size=2, max_size=128), st.integers(0, 255))
def test_unknown_property_keeps_opaque_tail(payload, tag):
    decoded = appmsg.decode_message(bytes([12, tag]) + payload, sizes={})
    assert decoded["properties"] == [{
        "propertyId": payload[0], "channel": payload[1],
        "value": payload[2:], "known": False,
    }]


@given(st.binary(max_size=128))
@example(b"\x03\x00" + bytes(64))
def test_adopt_response_requires_header_and_both_keys(body):
    if len(body) < 66 or body[0] != 3:
        with pytest.raises(ValueError):
            decode_adopt_response(body)
    else:
        assert decode_adopt_response(body) == (body[1], body[2:34], body[34:66])
