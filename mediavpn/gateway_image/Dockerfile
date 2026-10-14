FROM traefik:v3.3.7 AS proxy
FROM python:3.12-alpine
RUN apk add --no-cache iptables iproute2 ca-certificates
RUN pip install --no-cache-dir 'cryptography>=44,<50'
COPY --from=proxy /usr/local/bin/traefik /usr/local/bin/traefik
WORKDIR /opt/gateway
COPY config.py observer.py natpmp.py ./
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python3", "/opt/gateway/observer.py"]
