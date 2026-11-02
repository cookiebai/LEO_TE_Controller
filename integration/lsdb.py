"""Read the exact /lsdb.json schema shipped in weixing1020.tar."""
import ipaddress
import math
import re

import networkx as nx


NODE = re.compile(r"L(\d{2})(\d{2})$")


def coordinates(name):
    match = NODE.fullmatch(name)
    if not match:
        raise ValueError(f"Unsupported task-1 node name: {name}")
    row, col = map(int, match.groups())
    # frr-auto66.sh in the supplied image explicitly enforces this grid.
    if not (1 <= row <= 6 and 1 <= col <= 11):
        raise ValueError(f"Node outside the supplied path tuner's 6x11 grid: {name}")
    return row, col


def node_name(row, col):
    name = f"L{row:02d}{col:02d}"
    coordinates(name)
    return name


def positive(value, label, zero=False):
    number = float(value)
    if not math.isfinite(number) or (number < 0 if zero else number <= 0):
        raise ValueError(f"Invalid {label}: {value}")
    return number


def _identity(lsp):
    locator = lsp.get("srv6Locator", {})
    prefix = locator.get("prefix")
    if prefix:
        addr = ipaddress.IPv6Network(prefix).network_address.exploded.split(":")
        if addr[0] == "fc00":
            return node_name(int(addr[1], 16), int(addr[2], 16))
    for reach in lsp.get("ipv6Reach", []):
        if reach.get("down") or reach.get("external"):
            continue
        network = ipaddress.IPv6Network(reach["prefix"])
        groups = network.network_address.exploded.split(":")
        if network.prefixlen == 128 and groups[:2] == ["fc00", "0000"]:
            return node_name(int(groups[2], 16), int(groups[-1], 16))
    return None


def lsdb_areas(raw):
    """Accept explicit FRR area arrays, including a single default-VRF wrapper.

    Do not infer connectivity or merge unrelated VRFs. Report the actual keys
    instead of leaking an unhelpful KeyError('areas') at the field site.
    """
    candidates = []

    def visit(value, path, depth=0):
        if not isinstance(value, dict) or depth > 4:
            return
        for key in ('areas', 'area'):
            areas = value.get(key)
            if isinstance(areas, list) and all(isinstance(a, dict) and 'levels' in a for a in areas):
                candidates.append((path + '.' + key, areas))
                return
        for key, child in value.items():
            if isinstance(child, dict):
                visit(child, path + '.' + key, depth + 1)

    visit(raw, 'lsdb')
    if len(candidates) == 1:
        return candidates[0][1]
    if len(candidates) > 1:
        default = [item for item in candidates if '.default.' in item[0]]
        if len(default) == 1:
            return default[0][1]
        raise ValueError(f'Multiple FRR area/VRF arrays: {[p for p, _ in candidates]}; '
                         'select the intended IS-IS instance; not merging topologies')
    keys = list(raw)[:12] if isinstance(raw, dict) else type(raw).__name__
    raise ValueError(f'FRR LSDB has no supported areas/area array; actual keys={keys}. '
                     'Check IS-IS status and collect raw /lsdb.json and vtysh JSON')


def parse_lsdb(document):
    """Keep directed advertisements; never infer reverse links or grid edges."""
    lsps = [lsp for area in lsdb_areas(document.get('lsdb', {}))
            for level in area["levels"] if int(level["id"]) == 2
            for lsp in level["lsps"] if int(lsp.get("holdtime", 1)) > 0]
    graph = nx.DiGraph()
    aliases = {}
    identified = []
    warnings = []
    for lsp in lsps:
        name = _identity(lsp)
        if not name:
            warnings.append(f"unmapped LSP {lsp['lsp']['id']}")
            continue
        row, col = coordinates(name)
        locator = lsp.get("srv6Locator", {})
        end = locator.get("subtlvs", {}).get("srv6EndSid", {})
        sid = end.get("sidValue")
        ready = bool(sid and end.get("endpointBehavior") == "End")
        if ready and ipaddress.IPv6Address(sid) != ipaddress.IPv6Address(f"fc00:{row:x}:{col:x}::"):
            raise ValueError(f"{name}: advertised End SID differs from native path tuner's SID")
        attrs = dict(loopback=f"fc00:0:{row:x}::{col:x}",
                     locator=locator.get("prefix"), end_sid=sid, srv6_ready=ready)
        # Multiple fragments may describe the same node; preserve capabilities.
        if name not in graph or ready:
            graph.add_node(name, **attrs)
        for alias in (lsp["lsp"]["id"].split(".00-")[0], lsp.get("hostname"),
                      f"0001.{row:04x}.{col:04x}.00"):
            if alias:
                previous = aliases.setdefault(alias.lower(), name)
                if previous != name:
                    raise ValueError(f"Conflicting LSDB identity: {alias}")
        identified.append((name, lsp))
    for src, lsp in identified:
        if any(mt.get("overloadBit") for mt in lsp.get("mt", [])
               if mt.get("mtid") == 2):
            warnings.append(f"{src}: IPv6 overload, outgoing transit links excluded")
            continue
        for reach in lsp.get("extReach", []):
            if reach.get("mtName", "ipv6-unicast") != "ipv6-unicast":
                continue
            dst = aliases.get(reach["id"].lower())
            if not dst:
                warnings.append(f"{src}: unresolved adjacency {reach['id']}")
                continue
            metric = positive(reach["metric"], "IGP metric", zero=True)
            if metric >= 16777215 or src == dst:
                continue
            if graph.has_edge(src, dst):
                raise ValueError(f"Parallel advertisements for {src}->{dst}; explicit link IDs required")
            graph.add_edge(src, dst, igp_metric=metric,
                           advertised_endx=[s["sid"] for s in reach.get("srv6EndXSID", [])])
    if not graph:
        raise ValueError("LSDB has no identifiable live Level-2 nodes")
    # The wrapper's neighbors describe ONLY the exporting node.
    exporter = document.get("self")
    for neighbor in document.get("neighbors", []):
        dst = aliases.get(neighbor["name"].lower())
        if exporter in graph and dst and graph.has_edge(exporter, dst):
            graph[exporter][dst]["local_iface"] = neighbor["interface"].split("@")[0]
    return graph, warnings


def port_to(config, peer):
    """/cfg.sh is JSON in this image; port.linkid contains both endpoints."""
    own = config["name"]
    matches = [port for port in config["ports"].values()
               if any(link in (own + peer, peer + own) for link in port.get("linkid", []))]
    if len(matches) != 1:
        raise ValueError(f"Expected one /cfg.sh port for {own}->{peer}, got {len(matches)}")
    return matches[0]


def qdisc_metrics(records, text):
    roots = [q for q in records if q.get("root") and q["kind"] not in
             ("noqueue", "clsact", "ingress", "mq")]
    if len(roots) != 1 or "backlog" not in roots[0]:
        raise ValueError("No measurable single root qdisc; configure the actual egress queue")
    root = roots[0]
    result = {"queue_bytes": positive(root["backlog"], "backlog bytes", zero=True),
              "drops_total": root.get("drops", 0), "qdisc_handle": root["handle"]}
    # Root statistics already account for children: summing them double counts.
    rates = re.findall(r"\brate\s+([\d.]+)([KMGT]?)bit\b", text, re.I)
    if rates:
        scales = {"": 1e-6, "K": 1e-3, "M": 1, "G": 1e3, "T": 1e6}
        result["capacity_mbps"] = min(float(n) * scales[u.upper()] for n, u in rates)
    delays = re.findall(r"\bdelay\s+([\d.]+)(us|ms|s)\b", text)
    if delays:
        result["delay_ms"] = sum(float(n) * {"us": .001, "ms": 1, "s": 1000}[u]
                                 for n, u in delays)
    return result


def attach_metrics(graph, records, default_capacity=None, default_delay=None, allow_missing=False):
    """Explicit units only. Missing queue is different from a measured zero."""
    by_edge = {}
    for record in records:
        edge = (record["src"], record["dst"])
        if edge in by_edge:
            raise ValueError(f"Duplicate metrics for {edge}")
        by_edge[edge] = record
    missing = []
    for src, dst, data in graph.edges(data=True):
        record = by_edge.get((src, dst), {})
        values = {"capacity": record.get("capacity_mbps", default_capacity),
                  "delay": record.get("delay_ms", default_delay),
                  "queue": record.get("queue_bytes")}
        absent = [key for key, value in values.items() if value is None]
        if absent:
            missing.append(f"{src}->{dst}: {','.join(absent)}")
            if not allow_missing:
                continue
            # Only used by explicitly selected offline/demo mode.
            values = {"capacity": values["capacity"] if values["capacity"] is not None else 16,
                      "delay": values["delay"] if values["delay"] is not None else 10,
                      "queue": values["queue"] if values["queue"] is not None else 0}
        for key, value in values.items():
            data[key] = positive(value, key, zero=key != "capacity")
        data["metrics_source"] = record.get("source", "explicit defaults / offline demo")
        data["metrics_timestamp"] = record.get("timestamp")
        for key in ('qlen_packets', 'drops_total', 'overlimits_total', 'interface', 'namespace',
                    'measured', 'queue_poll_s', 'age_seconds', 'qdisc'):
            if key in record:
                data[key] = record[key]
    if missing and not allow_missing:
        raise ValueError(f"Missing link metrics ({len(missing)} links), e.g. {missing[:5]}")
    return missing
