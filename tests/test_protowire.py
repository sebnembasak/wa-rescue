import pytest
from warescue import protowire as pw


@pytest.mark.parametrize("n", [0, 1, 127, 128, 300, 2**32, 2**63 - 1])
def test_varint_roundtrip(n):
    assert pw.read_varint(pw.encode_varint(n), 0) == (n, len(pw.encode_varint(n)))


def test_nested_find():
    msg = pw.encode_field(1, 5) + pw.encode_field(3, pw.encode_field(1, b"\xaa" * 16))
    fields = pw.decode(msg)
    assert pw.find(fields, 1).value == 5
    assert pw.find(fields, 3, 1).value == b"\xaa" * 16
    assert pw.find(fields, 9) is None


def test_truncated_input_rejected():
    with pytest.raises(pw.ProtoError):
        pw.decode(pw.encode_field(2, b"hello")[:-1])
