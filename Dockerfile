FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY mediavpn ./mediavpn
RUN pip install --no-cache-dir . && useradd --uid 1000 --create-home mediavpn && mkdir /data && chown mediavpn:mediavpn /data
USER mediavpn
EXPOSE 18802
CMD ["uvicorn", "mediavpn.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "18802", "--workers", "1", "--no-access-log"]