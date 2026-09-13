import hmac
import base64
import hashlib
import time


class AuthenticationError(Exception):
    """Authentication error"""

    pass


class AuthManager:
    """
    Unified authentication manager.
    Generates and verifies the (client_id, device_id, token) triple using HMAC-SHA256.
    The token carries only the signature and timestamp, never the plaintext client_id/device_id,
    which are passed separately on connect.
    MQTT: client_id: client_id, username: device_id, password: token
    WebSocket: header:{Device-ID: device_id, Client-ID: client_id, Authorization: Bearer token, ......}
    """

    def __init__(self, secret_key: str, expire_seconds: int = 60 * 60 * 24 * 30):
        if not expire_seconds or expire_seconds < 0:
            self.expire_seconds = 60 * 60 * 24 * 30
        else:
            self.expire_seconds = expire_seconds
        self.secret_key = secret_key

    def _sign(self, content: str) -> str:
        """HMAC-SHA256 signature, urlsafe Base64 encoded."""
        sig = hmac.new(
            self.secret_key.encode("utf-8"), content.encode("utf-8"), hashlib.sha256
        ).digest()
        return base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")

    def generate_token(self, client_id: str, username: str) -> str:
        """
        Generate a token.
        Args:
            client_id: device connection ID
            username: device username (usually the deviceId)
        Returns:
            str: the token
        """
        ts = int(time.time())
        content = f"{client_id}|{username}|{ts}"
        signature = self._sign(content)
        # the token holds only the signature and timestamp, no plaintext
        token = f"{signature}.{ts}"
        return token

    def verify_token(self, token: str, client_id: str, username: str) -> bool:
        """
        Verify a token.
        Args:
            token: token sent by the client
            client_id: client_id used for the connection
            username: username used for the connection
        """
        try:
            sig_part, ts_str = token.split(".")
            ts = int(ts_str)
            if int(time.time()) - ts > self.expire_seconds:
                return False  # expired

            expected_sig = self._sign(f"{client_id}|{username}|{ts}")
            if not hmac.compare_digest(sig_part, expected_sig):
                return False

            return True
        except Exception:
            return False
