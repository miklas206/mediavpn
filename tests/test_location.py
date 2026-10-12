from mediavpn.location import ExitLocation, MAX_BYTES


def test_provider_feed_longest_prefix_and_ip_changes():
    calls = []
    def fetch():
        calls.append(True)
        return b'# geofeed\n8.0.0.0/8,US\n8.8.8.0/24,DK\n1.1.1.0/24,SE\ninvalid,DE\n9.9.9.0/24,invalid\n'
    location = ExitLocation(fetch=fetch)
    assert location.country('8.8.8.8') == 'DK'
    assert location.country('1.1.1.1') == 'SE'
    assert location.country('8.1.1.1') == 'US'
    assert location.country('9.9.9.9') is None
    assert location.country('192.168.1.1') is None
    assert location.country('invalid') is None
    assert len(calls) == 1


def test_expired_feed_failure_returns_unknown_and_retries():
    now = [1]
    calls = []
    def fetch():
        calls.append(True)
        if len(calls) in (2, 3):
            raise OSError('offline')
        return b'8.8.8.0/24,DK\n'
    location = ExitLocation(fetch=fetch, clock=lambda: now[0])
    assert location.country('8.8.8.8') == 'DK'
    now[0] += 21601
    assert location.country('8.8.8.8') is None
    assert location.country('8.8.8.8') is None
    assert len(calls) == 2
    now[0] += 61
    assert location.country('8.8.8.8') is None
    now[0] += 61
    assert location.country('8.8.8.8') == 'DK'


def test_invalid_or_oversized_feed_is_unknown():
    for raw in [b'broken', b'8.8.8.0/24,<script>', b'\xff', b'x' * (MAX_BYTES + 1)]:
        assert ExitLocation(fetch=lambda: raw).country('8.8.8.8') is None
