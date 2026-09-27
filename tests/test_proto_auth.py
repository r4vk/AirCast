from __future__ import annotations

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from aircast.cast import proto
from aircast.cast.auth import SelfSignedProvider


def test_cast_message_roundtrip_string():
    msg = proto.CastMessage("sender-0", "receiver-0", "urn:x-cast:test", payload_utf8='{"a":1}')
    decoded = proto.CastMessage.decode(msg.encode())
    assert decoded == msg
    assert decoded.payload_type == proto.PAYLOAD_STRING


def test_cast_message_roundtrip_binary():
    msg = proto.CastMessage("s", "d", "ns", payload_binary=b"\x00\x01\xff")
    decoded = proto.CastMessage.decode(msg.encode())
    assert decoded.payload_binary == b"\x00\x01\xff"
    assert decoded.payload_utf8 is None


def test_frame_has_big_endian_length_prefix():
    msg = proto.CastMessage("s", "d", "ns", payload_utf8="x")
    framed = proto.frame(msg)
    assert int.from_bytes(framed[:4], "big") == len(framed) - 4


def _challenge(nonce: bytes | None, hash_alg: int) -> bytes:
    inner = proto._field_varint(1, proto.SIG_RSASSA_PKCS1V15)
    if nonce is not None:
        inner += proto._field_bytes(2, nonce)
    inner += proto._field_varint(3, hash_alg)
    return proto._field_bytes(1, inner)


def test_decode_empty_challenge_uses_defaults():
    challenge = proto.decode_auth_challenge(proto._field_bytes(1, b""))
    assert challenge is not None
    assert challenge.sender_nonce is None
    assert challenge.hash_algorithm == proto.HASH_SHA1


def _parse_response(data: bytes) -> dict:
    (num, _w, body), = list(proto.iter_fields(data))
    assert num == 2
    out: dict = {"intermediates": []}
    for fnum, _fw, value in proto.iter_fields(body):
        if fnum == 3:
            out["intermediates"].append(value)
        else:
            out[fnum] = value
    return out


def test_selfsigned_signature_verifies_over_nonce_and_peer_cert(tmp_path):
    provider = SelfSignedProvider(tmp_path)
    nonce = b"0123456789abcdef"
    challenge = proto.decode_auth_challenge(_challenge(nonce, proto.HASH_SHA256))
    encoded = proto.encode_auth_response(provider.respond(challenge))
    response = _parse_response(encoded)

    device_cert = x509.load_der_x509_certificate(response[2])
    device_cert.public_key().verify(
        response[1], nonce + provider.peer_cert_der, padding.PKCS1v15(), hashes.SHA256()
    )
    assert response[5] == nonce
    assert len(response["intermediates"]) == 1


def test_selfsigned_material_is_persisted(tmp_path):
    first = SelfSignedProvider(tmp_path).peer_cert_der
    second = SelfSignedProvider(tmp_path).peer_cert_der
    assert first == second
