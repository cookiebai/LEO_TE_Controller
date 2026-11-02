FROM leo-te-controller:field-20261029
ADD node-payload.tar /
COPY merge-manifest.json setup-runtime.py /opt/leo-te-merge/
COPY --chmod=755 leo-te-merged-entrypoint /usr/local/bin/leo-te-merged-entrypoint
RUN python3 /opt/leo-te-merge/setup-runtime.py && \
    ldconfig && \
    python3 -c 'import redis, yaml, scipy, networkx, numpy' && \
    bash -n /auto66-lite.sh && bash -n /queue-probe.sh && \
    /usr/lib/frr/zebra --version && /usr/lib/frr/isisd --version && \
    leo-te help
LABEL org.leo-te.merge.controller="sha256:563adbf61556c6f83323a0a90ffd62e011544fc9b24353fd30fcc2d850724a12" \
      org.leo-te.merge.node="sha256:37ef9dd6e917dc5888a9faf5ff1063a79e42ede859200f62503fc23a668e79e1" \
      org.leo-te.merge.policy="controller-base; node files added verbatim; conflicting originals preserved"
EXPOSE 2601 2608
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/leo-te-merged-entrypoint"]
CMD ["serve"]
