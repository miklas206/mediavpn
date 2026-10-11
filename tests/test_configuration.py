import base64

import pytest

from mediavpn.configuration import parse_wireguard

KEY = base64.b64encode(bytes(range(32))).decode()
CONFIG = f"[Interface]\nPrivateKey={KEY}\nAddress=10.2.0.2/32\nDNS=10.2.0.1\n[Peer]\nPublicKey={KEY}\nAllowedIPs=0.0.0.0/0\nEndpoint=1.1.1.1:51820\n"


def test_valid_configuration():
    normalized, summary = parse_wireguard(CONFIG)
    assert summary["endpoint"] == "1.1.1.1"
    assert "PrivateKey" in normalized
    assert KEY not in str(summary)


@pytest.mark.parametrize("text", [CONFIG + "PostUp=touch /tmp/pwn\n", CONFIG.replace("1.1.1.1", "127.0.0.1"), CONFIG.replace("0.0.0.0/0", "10.0.0.0/8"), CONFIG.replace(KEY, "invalid"), CONFIG + "[Peer]\n", CONFIG + "x" * 17000])
def test_reject_unsafe_configuration(text):
    with pytest.raises(ValueError):
        parse_wireguard(text)