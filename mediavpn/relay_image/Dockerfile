FROM alpine:3.22
RUN apk add --no-cache python3 iproute2 iptables wireguard-tools wireguard-go
COPY runtime.py plex_forward.py /opt/relay/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python3", "/opt/relay/runtime.py"]
