FROM leo-te-controller:field-20261029 AS math-libs
RUN mkdir /mini-libs && cd /usr/local/lib/python3.10/dist-packages && \
    for item in numpy numpy.libs numpy-*.dist-info scipy scipy.libs scipy-*.dist-info \
      networkx networkx-*.dist-info yaml _yaml PyYAML-*.dist-info redis redis-*.dist-info \
      async_timeout async_timeout-*.dist-info; do \
        if [ -e "$item" ]; then cp -a "$item" /mini-libs/; fi; done && \
    find /mini-libs -type d \( -name tests -o -name __pycache__ -o -name doc \) -prune -exec rm -rf '{}' +

FROM ubuntu:22.04 AS runtime
ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 libstdc++6 tini ca-certificates procps iproute2 iputils-ping less \
    && rm -rf /var/lib/apt/lists/*
COPY --from=math-libs /mini-libs/ /usr/local/lib/python3.10/dist-packages/
COPY --from=math-libs /usr/local/bin/docker /usr/local/bin/docker
COPY --from=math-libs /usr/share/doc/leo-te-docker-cli/ /usr/share/doc/leo-te-docker-cli/
COPY --from=math-libs /app/bin/leo-te-queue-probe /app/bin/leo-te-queue-probe
RUN python3 -c 'import numpy, scipy, networkx, yaml, redis; from scipy.optimize import milp' && \
    mkdir -p /data /run && echo control > /run/leo-te-role

FROM runtime
WORKDIR /app
COPY app/ /app/
RUN printf '#!/bin/sh\nexec python3 -m mini_acceptance.cli "$@"\n' > /usr/local/bin/mini && \
    chmod 755 /usr/local/bin/mini && \
    printf '\nPS1="mini-test:\\w\\$ "\n' >> /etc/bash.bashrc && test ! -e /cfg.sh
ENV MINI_DATA_DIR=/data
ENTRYPOINT ["/usr/bin/tini", "--", "mini"]
CMD ["serve"]
