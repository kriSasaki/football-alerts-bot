import base64

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid01


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


keys = Vapid01()
keys.generate_keys()

public_key = keys.public_key.public_bytes(
    encoding=serialization.Encoding.X962,
    format=serialization.PublicFormat.UncompressedPoint,
)
private_value = keys.private_key.private_numbers().private_value.to_bytes(32, "big")

print("WEB_PUSH_VAPID_PUBLIC_KEY=" + _b64url(public_key))
print("WEB_PUSH_VAPID_PRIVATE_KEY=" + _b64url(private_value))
