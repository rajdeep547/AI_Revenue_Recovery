import hashlib
import hmac


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Verify Razorpay's X-Razorpay-Signature header.

    Razorpay computes HMAC-SHA256 of the raw request body using the
    webhook secret, hex-encoded. Constant-time compare to avoid timing
    side-channels.
    """
    if not signature:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
