FROM --platform=$BUILDPLATFORM python:3.12-slim-bookworm AS capture-builder
RUN pip install --no-cache-dir ziglang==0.14.1
COPY tools/sx1302/superlink/bridge/extraction/native/capture.c /build/capture.c
RUN python -m ziglang cc -target arm-linux-gnueabihf.2.30 -mcpu=cortex_a7 \
    -shared -fPIC -O2 -s -Wall -Werror /build/capture.c -o /build/capture-armhf.so -ldl -pthread

FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends libsodium23 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY tools/sx1302/superlink ./tools/sx1302/superlink
COPY --from=capture-builder /build/capture-armhf.so ./tools/sx1302/superlink/bridge/extraction/native/capture-armhf.so
RUN pip install --no-cache-dir .
RUN useradd --uid 10001 --create-home superlink && mkdir /data && chown superlink:superlink /data
USER superlink
EXPOSE 8099
WORKDIR /data
ENTRYPOINT ["superlink-bridged"]
CMD ["--config", "/data/superlink_bridge.yaml"]
