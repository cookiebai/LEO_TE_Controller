FROM ubuntu:22.04 AS engine-build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential cmake libhiredis-dev libyaml-cpp-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY CMakeLists.txt ./
COPY cpp_te_engine ./cpp_te_engine
RUN cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j2

FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip redis-server docker.io iproute2 ca-certificates tini \
    libhiredis0.14 libyaml-cpp0.7 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir -r requirements.txt
COPY --from=engine-build /src/build/te_engine /app/build/te_engine
COPY . /app
RUN mkdir -p /output /run/leo-te
ENTRYPOINT ["/usr/bin/tini", "--", "python3", "-m", "integration.controller"]
CMD ["--help"]
