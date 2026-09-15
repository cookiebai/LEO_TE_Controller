import argparse
import time
import json
import yaml
import redis
import hashlib
import networkx as nx
import numpy as np
from itertools import islice
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import lil_matrix

class LyapunovSolver:
    def __init__(self, config_path):
        with open(config_path, 'r') as f:
            self.config = yaml.safe_load(f)
        
        redis_cfg = self.config['redis']
        self.r = redis.Redis(host=redis_cfg['host'], port=redis_cfg['port'], decode_responses=True)
        self.queue_name = redis_cfg['te_queue_name']
        
        l_cfg = self.config['lyapunov_engine']
        self.V = l_cfg['v_parameter']
        self.K = l_cfg['max_k_paths']
        self.rho = l_cfg['capacity_rho']
        self.classes = l_cfg['traffic_classes']
        self.sid_prefix = self.config['network']['srv6_locator_prefix']
        self.policy_state_key = l_cfg.get('policy_state_key', 'te:policy:last_signature')
        self.policy_details_key = l_cfg.get('policy_details_key', 'te:policy:desired')
        self.policy_changed_at_key = l_cfg.get('policy_changed_at_key', 'te:policy:changed_at')
        self.max_policy_queue_len = int(l_cfg.get('max_policy_queue_len', 1000))
        self.min_policy_dwell_seconds = float(l_cfg.get('min_policy_dwell_seconds', 20))
        self.max_sid_depth = int(
            self.config.get('te_engine', {}).get('max_sid_depth', 16)
        )

        self._replace_policy_script = self.r.register_script(
            """
            local queue = KEYS[1]
            local flow_id = ARGV[1]
            local new_policy = ARGV[2]
            local max_len = tonumber(ARGV[3])
            local values = redis.call('LRANGE', queue, 0, -1)
            redis.call('DEL', queue)
            for _, raw in ipairs(values) do
                local ok, decoded = pcall(cjson.decode, raw)
                if (not ok) or decoded['flow_id'] ~= flow_id then
                    redis.call('RPUSH', queue, raw)
                end
            end
            redis.call('RPUSH', queue, new_policy)
            if max_len > 0 then
                redis.call('LTRIM', queue, -max_len, -1)
            end
            return redis.call('LLEN', queue)
            """
        )

    def _policy_signature(self, policy: dict) -> str:
        """生成稳定签名，避免同一路径因权重浮动反复入队。

        当前 SRv6 sender 最终安装的是单条内核 route，weight 不会体现在
        Linux 路由行为里；把 weight 纳入签名只会让微小求解抖动触发重复下发。
        """
        payload = {
            "src": policy["src"],
            "dst": policy["dst"],
            "path": policy["path"],
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _replace_queued_policy(self, policy: dict) -> None:
        """Atomically keep only the newest queued policy for this flow."""
        self._replace_policy_script(
            keys=[self.queue_name],
            args=[
                policy["flow_id"],
                json.dumps(policy, separators=(",", ":")),
                self.max_policy_queue_len,
            ],
        )

    def _publish_policy_if_changed(self, policy: dict) -> bool:
        """只有策略变化时才入队，并限制队列长度，防止下发器追旧策略。"""
        state_field = policy["flow_id"]
        signature = self._policy_signature(policy)
        old_signature = self.r.hget(self.policy_state_key, state_field)
        if old_signature == signature:
            return False

        now = time.time()
        changed_at = float(self.r.hget(self.policy_changed_at_key, state_field) or 0)
        if (
            old_signature
            and self.min_policy_dwell_seconds > 0
            and now - changed_at < self.min_policy_dwell_seconds
        ):
            return False

        pipe = self.r.pipeline(transaction=True)
        pipe.hset(self.policy_state_key, state_field, signature)
        pipe.hset(
            self.policy_details_key,
            state_field,
            json.dumps(policy, separators=(",", ":")),
        )
        pipe.hset(self.policy_changed_at_key, state_field, f"{now:.6f}")
        pipe.execute()
        self._replace_queued_policy(policy)
        return True

    def _select_installable_policy(self, flow_id: str, routes: list[dict]) -> dict | None:
        """
        当前下发器以目的地址安装单条 SRv6 route，不能表达多路径分流。
        因此每条 flow 只发布最大分配比例的路径，避免同一目的被多条策略反复覆盖。
        """
        if not routes:
            return None

        route = max(routes, key=lambda item: item.get('allocated_bw', 0.0))
        path = route['path']
        return {
            "flow_id": flow_id,
            "src": path[0],
            "dst": path[-1],
            "weight": route['weight_ratio'],
            "path": path,
        }

    def fetch_network_state(self):
        """
        从 Redis 构建有向图，包含延迟、容量和队列状态 Q_e(t)

        数据源：
        1. topo:link:* -> 拓扑结构（节点、边、延迟、容量）
        2. telemetry:queue:<node_id> -> 真实队列积压数据

        链路 src->dst 的队列积压 = telemetry:queue:<src>[<local_iface>]
        由于 veth 命名可能不统一，优先匹配 local_iface，其次尝试常见命名规则
        """
        G = nx.DiGraph()
        keys = self.r.keys("topo:link:*")

        for key in keys:
            data = self.r.hgetall(key)
            if not data:
                continue

            # 检查链路状态
            if data.get('status', 'UP') != 'UP':
                continue

            u = data['src']
            v = data['dst']
            link_id = key.replace("topo:link:", "")

            # 从 telemetry:queue:<src> 获取该链路的队列积压
            # 队列数据的 key 格式: telemetry:queue:<node_id>
            # field 为本地网卡名（可能是 eth1, veth_xxx 等）
            queue_backlog = self._fetch_queue_for_link(u, v, link_id, data)

            # 提取延迟和容量
            delay = float(data.get('delay', 10.0))  # ms
            capacity = float(data.get('capacity', 1000.0))  # Mbps

            # 额外提取 utilization 作为参考（但主要用队列积压作为拥塞指标）
            utilization = float(data.get('utilization', 0.0))  # 百分比

            G.add_edge(u, v,
                       delay=delay,
                       capacity=capacity,
                       queue=queue_backlog,  # Q_e(t) - 核心指标
                       utilization=utilization,
                       link_id=link_id)

        return G

    def _fetch_queue_for_link(self, src_node: str, dst_node: str, link_id: str, link_data: dict) -> float:
        """
        从 telemetry:queue:<src_node> 中获取该链路的队列积压

        匹配策略：
        1. 先尝试 local_iface（如果存在）
        2. 尝试直接匹配目的节点名（如 eth_sat2）
        3. 尝试部分匹配（如 veth_*_<dst>*）
        4. 如果都匹配不到，返回 0.0 并打印告警日志
        """
        queue_key = f"telemetry:queue:{src_node}"

        try:
            # 获取该节点的所有队列数据
            all_queues = self.r.hgetall(queue_key)

            if not all_queues:
                return 0.0

            # 策略 1: 尝试 local_iface（来自拓扑数据）
            local_iface = link_data.get('local_iface')
            if local_iface and local_iface in all_queues:
                return float(all_queues[local_iface])

            # 策略 2: 尝试直接用 dst 节点名匹配
            # 常见命名: eth_<dst>, veth_<src>_<dst>, <dst>-<src>
            candidates = [
                f"eth_{dst_node}",
                f"veth_{dst_node}",
                f"{dst_node}",
            ]

            for iface in candidates:
                if iface in all_queues:
                    return float(all_queues[iface])

            # 策略 3: 模糊匹配 veth 接口
            # 格式: veth_<hash>_<peer> 或类似的
            dst_lower = dst_node.lower()
            for iface, value in all_queues.items():
                if dst_lower in iface.lower() and 'queue' not in iface:
                    return float(value)

            # 如果都匹配不到，返回 0.0 并告警
            print(f"[警告] 链路 {src_node} -> {dst_node} (queue_key: {queue_key}) 未找到匹配的队列数据")
            print(f"       可用的队列字段: {list(all_queues.keys())}")
            return 0.0

        except Exception as e:
            print(f"[警告] 读取队列数据失败 ({queue_key}): {e}")
            return 0.0

    def get_k_shortest_paths(self, G, source, target, k=3):
        """利用 Yen's 算法获取 K 条备选无环路径"""
        try:
            return list(islice(nx.shortest_simple_paths(G, source, target, weight='delay'), k))
        except nx.NetworkXNoPath:
            return []

    def solve_lyapunov_mcf(self, G, flows):
        """Globally select one installable path per flow.

        The data plane installs one destination route per flow, so fractional
        MCF output cannot be represented faithfully.  Build a small binary
        min-max model over the K candidate paths instead: the primary objective
        minimizes the highest normalized offered load on any directed link,
        while the Lyapunov delay/queue term selects among equally balanced
        solutions.  A deterministic marginal-cost greedy pass is retained as a
        fallback when the integer solver cannot return a feasible incumbent.
        """
        if not G.edges or not flows:
            return None

        valid_flows = []
        for flow in flows:
            if flow['src'] not in G:
                print(f"[警告] 流 {flow['id']} 源节点 {flow['src']} 不在拓扑图中，跳过")
                continue
            if flow['dst'] not in G:
                print(f"[警告] 流 {flow['id']} 目的节点 {flow['dst']} 不在拓扑图中，跳过")
                continue
            paths = self.get_k_shortest_paths(
                G,
                flow['src'],
                flow['dst'],
                self.K,
            )
            paths = [
                path for path in paths
                if len(path) - 1 <= self.max_sid_depth
            ]
            if not paths:
                print(f"[警告] 流 {flow['id']} 找不到连通路径，跳过")
                continue
            valid_flows.append((flow, paths))

        if not valid_flows:
            return None

        valid_flows.sort(key=lambda item: item[0]['id'])
        edges = sorted(G.edges)
        edge_index = {edge: index for index, edge in enumerate(edges)}
        candidates = []
        candidates_by_flow = [[] for _ in valid_flows]
        for flow_index, (flow, paths) in enumerate(valid_flows):
            cls_cfg = self.classes[flow['class']]
            alpha_c = float(cls_cfg['alpha'])
            beta_c = float(cls_cfg['beta'])
            for path in paths:
                path_edges = list(zip(path[:-1], path[1:]))
                path_delay = sum(
                    float(G[u][v]['delay']) for u, v in path_edges
                )
                # queue_monitor reports bytes, while path_delay is in
                # milliseconds. Convert backlog/drop penalty bytes to the
                # equivalent serialization delay before combining both terms.
                # At 16 Mbps, for example, 1500 bytes correspond to 0.75 ms.
                path_queue = sum(
                    float(G[u][v]['queue'])
                    * 8.0
                    / (
                        max(1.0, float(G[u][v]['capacity']))
                        * 1000.0
                    )
                    for u, v in path_edges
                )
                path_cost = (
                    self.V * alpha_c * path_delay
                    + beta_c * path_queue
                )
                candidate_index = len(candidates)
                candidates_by_flow[flow_index].append(candidate_index)
                candidates.append({
                    'flow_index': flow_index,
                    'path': path,
                    'edges': path_edges,
                    'path_cost': path_cost,
                })

        variable_count = len(candidates) + 1
        max_load_variable = variable_count - 1
        constraint_count = len(valid_flows) + len(edges)
        matrix = lil_matrix((constraint_count, variable_count))
        lower = np.full(constraint_count, -np.inf)
        upper = np.zeros(constraint_count)

        for flow_index, candidate_indexes in enumerate(candidates_by_flow):
            lower[flow_index] = 1.0
            upper[flow_index] = 1.0
            for candidate_index in candidate_indexes:
                matrix[flow_index, candidate_index] = 1.0

        for candidate_index, candidate in enumerate(candidates):
            flow = valid_flows[candidate['flow_index']][0]
            demand_kbps = max(0.0, float(flow['demand']))
            for edge in candidate['edges']:
                capacity_kbps = max(
                    1.0,
                    float(G[edge[0]][edge[1]]['capacity'])
                    * 1000.0
                    * self.rho,
                )
                row = len(valid_flows) + edge_index[edge]
                matrix[row, candidate_index] = demand_kbps / capacity_kbps
        for edge_offset in range(len(edges)):
            matrix[
                len(valid_flows) + edge_offset,
                max_load_variable,
            ] = -1.0

        objective = np.zeros(variable_count)
        for index, candidate in enumerate(candidates):
            # Stable microscopic tie-break after the physical path cost.
            objective[index] = candidate['path_cost'] + index * 1e-7
        # A full unit of normalized peak load dominates any possible aggregate
        # delay/queue difference in this small experiment.
        objective[max_load_variable] = 1_000_000.0
        integrality = np.ones(variable_count)
        integrality[max_load_variable] = 0
        bounds = Bounds(
            np.zeros(variable_count),
            np.concatenate((np.ones(len(candidates)), [np.inf])),
        )
        solution = milp(
            c=objective,
            integrality=integrality,
            bounds=bounds,
            constraints=LinearConstraint(
                matrix.tocsr(),
                lower,
                upper,
            ),
            options={'time_limit': 20.0, 'mip_rel_gap': 0.0},
        )

        selected = {}
        if solution.x is not None:
            for flow_index, candidate_indexes in enumerate(candidates_by_flow):
                chosen = max(
                    candidate_indexes,
                    key=lambda index: solution.x[index],
                )
                if solution.x[chosen] < 0.5:
                    selected = {}
                    break
                selected[flow_index] = chosen

        if len(selected) != len(valid_flows):
            print(
                f"[警告] 全局单路径模型未返回可用解 "
                f"(status={solution.status}, message={solution.message})，"
                "回退到确定性增量选路"
            )
            selected = {}
            link_load_kbps = {edge: 0.0 for edge in edges}
            placement_order = sorted(
                range(len(valid_flows)),
                key=lambda index: (
                    -float(valid_flows[index][0]['demand']),
                    valid_flows[index][0]['id'],
                ),
            )
            for flow_index in placement_order:
                flow = valid_flows[flow_index][0]
                demand_kbps = max(0.0, float(flow['demand']))
                choices = []
                for candidate_index in candidates_by_flow[flow_index]:
                    candidate = candidates[candidate_index]
                    projected_peak = max(
                        [
                            (
                                link_load_kbps[edge] + demand_kbps
                            )
                            / (
                                max(
                                    1.0,
                                    float(G[edge[0]][edge[1]]['capacity'])
                                    * 1000.0
                                    * self.rho,
                                )
                            )
                            for edge in candidate['edges']
                        ]
                        + [
                            link_load_kbps[edge]
                            / (
                                max(
                                    1.0,
                                    float(G[edge[0]][edge[1]]['capacity'])
                                    * 1000.0
                                    * self.rho,
                                )
                            )
                            for edge in edges
                        ]
                    )
                    choices.append((
                        projected_peak,
                        candidate['path_cost'],
                        len(candidate['path']),
                        tuple(candidate['path']),
                        candidate_index,
                    ))
                chosen = min(choices)[-1]
                selected[flow_index] = chosen
                for edge in candidates[chosen]['edges']:
                    link_load_kbps[edge] += demand_kbps

        results = {}
        planned_load_kbps = {edge: 0.0 for edge in edges}
        for flow_index, candidate_index in selected.items():
            flow = valid_flows[flow_index][0]
            candidate = candidates[candidate_index]
            demand = max(0.0, float(flow['demand']))
            for edge in candidate['edges']:
                planned_load_kbps[edge] += demand
            results[flow['id']] = [{
                'path': candidate['path'],
                'allocated_bw': demand,
                'weight_ratio': 1.0,
                'objective': float(candidate['path_cost']),
            }]

        peak_utilization = max(
            planned_load_kbps[edge]
            / (
                max(
                    1.0,
                    float(G[edge[0]][edge[1]]['capacity'])
                    * 1000.0
                    * self.rho,
                )
            )
            for edge in edges
        )

        print(
            f"[Lyapunov] 全局单路径策略: {len(results)}/{len(flows)}, "
            f"候选路径 K={self.K}, 预测峰值利用率={peak_utilization:.3f}, "
            f"MILP状态={solution.status}"
        )
        return results

    def path_to_sid_list(self, path):
        """将节点路径转换为 SRv6 SID 列表 (倒序压栈)

        节点格式可能是:
        - Satellite_xxx (完整容器名)
        - fd00:xxxx::1 (已转换的IPv6)

        统一转换为仅哈希的 IPv6 格式: fd00:xxxx:xxxx::1
        """
        sid_list = []
        for node in path[1:]:
            # 提取哈希值
            clean_id = str(node).replace("Satellite_", "").replace("GroundStation_", "")
            clean_id = clean_id.replace("fd00:", "").replace("::1", "").replace(":", "").replace("_", "")
            # 格式化为 fd00:xxxx:xxxx::1
            if len(clean_id) == 8:
                sid_list.append(f"fd00:{clean_id[:4]}:{clean_id[4:]}::1")
            else:
                sid_list.append(f"fd00:{clean_id}::1")
        return sid_list

    def fetch_flows(self):
        """
        从 Redis 获取流量需求矩阵

        数据源：te:demands (JSON 格式)
        如果 Redis 中没有数据，返回 None（跳过该轮调度）
        """
        try:
            demands_str = self.r.get("te:demands")
            if demands_str:
                flows = json.loads(demands_str)
                print(f"[t] 从 Redis 读取到 {len(flows)} 条流量需求")
                return flows
            else:
                return None
        except Exception as e:
            print(f"[警告] 读取流量需求失败: {e}")
            return None

    def run(self, once=False):
        print("====== Lyapunov 差异化 TE 引擎启动 ======")
        print(f"配置: V={self.V}, K={self.K}, rho={self.rho}")
        print(f"流量类别权重:")
        for cls_name, cfg in self.classes.items():
            print(f"  {cls_name}: alpha={cfg['alpha']}, beta={cfg['beta']}")
        print("=" * 50)

        polling_ms = self.config['lyapunov_engine']['polling_interval_ms']

        while True:
            try:
                # 1. 抓取快照 Q(t) - 拓扑和队列状态
                G = self.fetch_network_state()

                if G.number_of_nodes() == 0:
                    print("[等待] 未检测到拓扑数据，等待数据平面注入...")
                    time.sleep(polling_ms / 1000.0)
                    continue

                print(f"\n[t] 检测到 {G.number_of_nodes()} 个节点, {G.number_of_edges()} 条链路")
                print(f"[t] 正在评估全网队列状态...")

                # 2. 从 Redis 获取当前需要调度的大流 (大象流)
                flows = self.fetch_flows()

                if not flows:
                    # 泛化逻辑：如果 Redis 中没有指定的流量需求，自动从真实拓扑中抽取节点生成背景流
                    active_nodes = list(G.nodes())
                    if len(active_nodes) >= 2:
                        import random
                        # 随机挑选两个真实存在的不同节点
                        src = random.choice(active_nodes)
                        dst = random.choice([n for n in active_nodes if n != src])
                        
                        # 动态生成这两颗卫星之间的流
                        flows = [
                            {'id': f'flow_ctrl_{src[-4:]}', 'src': src, 'dst': dst, 'class': 'class_1_urllc', 'demand': 10},
                            {'id': f'flow_data_{src[-4:]}', 'src': src, 'dst': dst, 'class': 'class_3_background', 'demand': 500}
                        ]
                        print(f"[t] 泛化模式激活: 随机抽取活跃节点 {src} -> {dst} 进行路由计算")
                    else:
                        print("[等待] 活跃节点不足 2 个，无法生成泛化流...")
                        time.sleep(polling_ms / 1000.0)
                        continue

                # 打印当前队列状态（用于调试）
                congested_links = []
                for u, v, data in G.edges(data=True):
                    if data['queue'] > 50000:  # 队列 > 50KB 视为拥塞
                        congested_links.append(f"{u}->{v}(Q={data['queue']})")
                if congested_links:
                    print(f"[t] 检测到拥塞链路: {congested_links}")

                # 3. 求解 Lyapunov 优化问题
                results = self.solve_lyapunov_mcf(G, flows)

                if results:
                    print("[t] 最优策略已生成，下发 SRv6 意图：")
                    published = 0
                    skipped = 0
                    for flow_id, routes in results.items():
                        for route in routes:
                            sid_list = self.path_to_sid_list(route['path'])
                            print(f"  -> 流 [{flow_id}] | 占比 {route['weight_ratio']:.0%} | 路径: {' -> '.join(route['path'])}")
                            print(f"     SID: {sid_list}")

                        policy_cmd = self._select_installable_policy(flow_id, routes)
                        if not policy_cmd:
                            continue

                        if self._publish_policy_if_changed(policy_cmd):
                            published += 1
                            print(f"     [publish] 已入队当前主路径，占比 {policy_cmd['weight']:.0%}")
                        else:
                            skipped += 1

                    print(
                        f"[t] 策略发布完成: 新增/变化 {published} 条, 未变化跳过 {skipped} 条, "
                        f"队列长度 {self.r.llen(self.queue_name)}"
                    )
                if once:
                    break

            except KeyboardInterrupt:
                print("\n收到停止信号，Lyapunov 引擎退出...")
                break
            except Exception as e:
                print(f"[错误] 主循环异常: {e}")
                if once:
                    raise

            time.sleep(polling_ms / 1000.0)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="OpenSN Lyapunov SRv6 TE solver")
    parser.add_argument(
        "--config",
        default="../config/controller.yaml",
        help="controller YAML path",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="compute and publish one stable policy snapshot, then exit",
    )
    args = parser.parse_args()
    solver = LyapunovSolver(args.config)
    solver.run(once=args.once)
