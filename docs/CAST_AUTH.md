# Cast device authentication

## How it works

When a Cast sender connects to a receiver (TLS, port from the `_googlecast._tcp` mDNS
record), it sends a `DeviceAuthMessage` challenge on the
`urn:x-cast:com.google.cast.tp.deviceauth` namespace. The receiver answers with:

- a **device certificate** plus intermediate chain, and
- a **signature** (RSASSA-PKCS1-v1_5 or PSS, SHA-1 or SHA-256) over the DER of the TLS
  certificate the receiver presented, prefixed with the sender nonce when one is sent.

Official senders (Android Cast SDK, Chrome, Google Home) check that the chain ends in
Google's Cast root CA. Many open-source senders do not verify it at all.

## Providers

### `selfsigned` (default)

AirCast generates a local CA, a device certificate and a TLS certificate on first start
and stores them in `<state_dir>/cast/`. Signatures are computed live and are
cryptographically valid, just not rooted in Google's CA.

- ✅ pychromecast, catt, Home Assistant, Music Assistant and similar senders
- ❌ stock Android/Chrome senders (they silently hide or refuse the device)

### `files`

Loads material you supply. AirCast ships none, downloads none and does not explain how
to obtain any. Make sure you have the right to use whatever you put here.

```yaml
cast_auth: files
cast_auth_dir: /data/cast-auth
```

Directory layout (PEM unless noted):

| File | Required | Content |
|---|---|---|
| `tls.crt`, `tls.key` | yes | TLS server certificate and key presented to senders |
| `device.crt` | yes | Device certificate |
| `intermediates.pem` | no | Intermediate certificate(s), leaf-to-root order |
| `device.key` | one of | Device private key; signatures are computed per challenge |
| `signatures.json` | one of | Precomputed signatures over `tls.crt`, selected by date |
| `crl.bin` | no | Binary CRL bundle forwarded to senders |

`signatures.json`:

```json
{
  "signatures": [
    {"not_before": "2026-01-01T00:00:00Z", "not_after": "2026-01-03T00:00:00Z",
     "signature": "<base64>"}
  ]
}
```

When no entry covers the current date, the receiver answers with an auth error and the
sender disconnects; the log says so.

## Recommendation

For official Android apps, point a DLNA controller at the AirCast renderer instead. It
needs no certificates and is not affected by sender-side changes.
