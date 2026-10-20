FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends libsodium23 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY tools/sx1302/superlink ./tools/sx1302/superlink
RUN pip install --no-cache-dir .
RUN useradd --uid 10001 --create-home superlink && mkdir /data && chown superlink:superlink /data
USER superlink
EXPOSE 8099
WORKDIR /data
ENTRYPOINT ["superlink-bridged"]
CMD ["--config", "/data/superlink_bridge.yaml"]
