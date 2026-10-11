import base64
import configparser
import ipaddress
import io


def parse_wireguard(text: str) -> tuple[str, dict]:
    if len(text.encode()) > 16384:
        raise ValueError("Konfigurationen er for stor")
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(text)
        if set(parser.sections()) != {"Interface", "Peer"} or parser.defaults():
            raise ValueError()
        allowed = {"Interface": {"PrivateKey", "Address", "DNS", "MTU"}, "Peer": {"PublicKey", "PresharedKey", "AllowedIPs", "Endpoint", "PersistentKeepalive"}}
        for section in parser.sections():
            if set(parser[section]) - allowed[section]:
                raise ValueError()
        for section, field in [("Interface", "PrivateKey"), ("Peer", "PublicKey")]:
            if len(base64.b64decode(parser[section][field], validate=True)) != 32:
                raise ValueError()
        if "PresharedKey" in parser["Peer"] and len(base64.b64decode(parser["Peer"]["PresharedKey"], validate=True)) != 32:
            raise ValueError()
        addresses = [ipaddress.ip_interface(value.strip()) for value in parser["Interface"]["Address"].split(",")]
        if not addresses:
            raise ValueError()
        routes = [ipaddress.ip_network(value.strip()) for value in parser["Peer"]["AllowedIPs"].split(",")]
        if ipaddress.ip_network("0.0.0.0/0") not in routes:
            raise ValueError()
        endpoint, port = parser["Peer"]["Endpoint"].rsplit(":", 1)
        address = ipaddress.ip_address(endpoint.strip("[]"))
        if not address.is_global or not 1 <= int(port) <= 65535:
            raise ValueError()
        for value in parser["Interface"].get("DNS", "").split(","):
            if value.strip():
                ipaddress.ip_address(value.strip())
        if "MTU" in parser["Interface"] and not 1280 <= int(parser["Interface"]["MTU"]) <= 1500:
            raise ValueError()
        if "PersistentKeepalive" in parser["Peer"] and not 0 <= int(parser["Peer"]["PersistentKeepalive"]) <= 65535:
            raise ValueError()
    except (ValueError, KeyError, configparser.Error, TypeError):
        raise ValueError("Ugyldig WireGuard-fil. Kun en fuld tunnel med IP-endpoint og sikre direktiver understottes.") from None
    output = io.StringIO()
    parser.write(output)
    return output.getvalue(), {"endpoint": str(address), "port": int(port), "addresses": [str(value) for value in addresses]}