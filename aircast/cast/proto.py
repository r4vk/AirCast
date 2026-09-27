"""Hand-rolled protobuf codec for the few Cast V2 messages a receiver needs.

Schema reference: Chromium components/cast_channel/proto/cast_channel.proto
"""

from __future__ import annotations

from dataclasses import dataclass, field

PROTOCOL_VERSION = 0  # CASTV2_1_0
PAYLOAD_STRING = 0
PAYLOAD_BINARY = 1

SIG_RSASSA_PKCS1V15 = 1
SIG_RSASSA_PSS = 2
HASH_SHA1 = 0
HASH_SHA256 = 1

# -- wire format ------------------------------------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    value &= 0xFFFFFFFFFFFFFFFF
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _field_varint(num: int, value: int) -> bytes:
    return _varint(num << 3) + _varint(value)


def _field_bytes(num: int, value: bytes) -> bytes:
    return _varint(num << 3 | 2) + _varint(len(value)) + value


def iter_fields(data: bytes):
    """Yield (field_number, wire_type, value); value is int for varints, bytes otherwise."""
    pos = 0
    while pos < len(data):
        key, pos = _read_varint(data, pos)
        num, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _read_varint(data, pos)
        elif wire == 2:
            length, pos = _read_varint(data, pos)
            value = data[pos : pos + length]
            if len(value) != length:
                raise ValueError("truncated field")
            pos += length
        elif wire == 1:
            value, pos = data[pos : pos + 8], pos + 8
        elif wire == 5:
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield num, wire, value


# -- messages ---------------------------------------------------------------------


@dataclass
class CastMessage:
    source_id: str
    destination_id: str
    namespace: str
    payload_utf8: str | None = None
    payload_binary: bytes | None = None
    protocol_version: int = PROTOCOL_VERSION

    @property
    def payload_type(self) -> int:
        return PAYLOAD_BINARY if self.payload_binary is not None else PAYLOAD_STRING

    def encode(self) -> bytes:
        out = _field_varint(1, self.protocol_version)
        out += _field_bytes(2, self.source_id.encode())
        out += _field_bytes(3, self.destination_id.encode())
        out += _field_bytes(4, self.namespace.encode())
        out += _field_varint(5, self.payload_type)
        if self.payload_binary is not None:
            out += _field_bytes(7, self.payload_binary)
        else:
            out += _field_bytes(6, (self.payload_utf8 or "").encode())
        return out

    @classmethod
    def decode(cls, data: bytes) -> CastMessage:
        values: dict[int, object] = {}
        for num, _wire, value in iter_fields(data):
            values[num] = value
        msg = cls(
            source_id=_str(values.get(2)),
            destination_id=_str(values.get(3)),
            namespace=_str(values.get(4)),
            protocol_version=int(values.get(1, 0)),  # type: ignore[arg-type]
        )
        if values.get(5) == PAYLOAD_BINARY:
            msg.payload_binary = bytes(values.get(7, b""))  # type: ignore[arg-type]
        else:
            msg.payload_utf8 = _str(values.get(6))
        return msg


def _str(value: object) -> str:
    return bytes(value).decode("utf-8") if isinstance(value, (bytes, bytearray)) else ""


@dataclass
class AuthChallenge:
    signature_algorithm: int = SIG_RSASSA_PKCS1V15
    sender_nonce: bytes | None = None
    hash_algorithm: int = HASH_SHA1


@dataclass
class AuthResponse:
    signature: bytes
    client_auth_certificate: bytes
    intermediate_certificates: list[bytes] = field(default_factory=list)
    signature_algorithm: int = SIG_RSASSA_PKCS1V15
    sender_nonce: bytes | None = None
    hash_algorithm: int = HASH_SHA1
    crl: bytes | None = None

    def encode(self) -> bytes:
        out = _field_bytes(1, self.signature)
        out += _field_bytes(2, self.client_auth_certificate)
        for cert in self.intermediate_certificates:
            out += _field_bytes(3, cert)
        out += _field_varint(4, self.signature_algorithm)
        if self.sender_nonce is not None:
            out += _field_bytes(5, self.sender_nonce)
        out += _field_varint(6, self.hash_algorithm)
        if self.crl is not None:
            out += _field_bytes(7, self.crl)
        return out


def decode_auth_challenge(data: bytes) -> AuthChallenge | None:
    """Parse a DeviceAuthMessage; return its challenge (or None if it carries none)."""
    for num, _wire, value in iter_fields(data):
        if num == 1:
            challenge = AuthChallenge()
            for cnum, _cwire, cvalue in iter_fields(bytes(value)):  # type: ignore[arg-type]
                if cnum == 1:
                    challenge.signature_algorithm = int(cvalue)  # type: ignore[arg-type]
                elif cnum == 2:
                    challenge.sender_nonce = bytes(cvalue)  # type: ignore[arg-type]
                elif cnum == 3:
                    challenge.hash_algorithm = int(cvalue)  # type: ignore[arg-type]
            return challenge
    return None


def encode_auth_response(response: AuthResponse) -> bytes:
    return _field_bytes(2, response.encode())


def encode_auth_error(error_type: int = 0) -> bytes:
    return _field_bytes(3, _field_varint(1, error_type))


def frame(message: CastMessage) -> bytes:
    body = message.encode()
    return len(body).to_bytes(4, "big") + body
