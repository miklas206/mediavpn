# Produce a stable dependency manifest: app code and release versions are not inputs.
FROM python:3.12-slim AS dependency-manifest
WORKDIR /manifest
COPY pyproject.toml ./
RUN python -c "import pathlib,tomllib; p=tomllib.loads(pathlib.Path('pyproject.toml').read_text()); r=sorted(set(p['project']['dependencies']+p['build-system']['requires'])); pathlib.Path('requirements.txt').write_text(chr(10).join(r)+chr(10))"

FROM python:3.12-slim
LABEL org.mediahub.resources.client-limits="1"
WORKDIR /app
COPY --from=dependency-manifest /manifest/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
RUN useradd --uid 1000 --create-home mediavpn && mkdir /data && chown mediavpn:mediavpn /data
COPY pyproject.toml ./
COPY mediavpn ./mediavpn
RUN chmod -R a+rX /app/mediavpn && pip install --no-cache-dir --no-deps --no-build-isolation --no-index .
USER mediavpn
EXPOSE 18802
CMD ["uvicorn", "mediavpn.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "18802", "--workers", "1", "--no-access-log"]
