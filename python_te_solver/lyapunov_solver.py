import time
import json
import yaml
import redis
import networkx as nx
import cvxpy as cp
import numpy as np
from itertools import islice

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
        """
        核心数学模型转化：求解差异化 Lyapunov 优化方程
        flows: list of dicts [{'id': 'f1', 'src': 's1', 'dst': 'd1', 'class': 'class_1_urllc', 'demand': 50}]
        """
        if not G.edges or not flows:
            return None

        # 1. 预计算所有流的 K 条备选路径
        flow_paths = {}
        for flow in flows:
            paths = self.get_k_shortest_paths(G, flow['src'], flow['dst'], self.K)
            if not paths:
                print(f"[警告] 流 {flow['id']} 找不到连通路径")
                return None
            flow_paths[flow['id']] = paths

        # 2. 定义 CVXPY 决策变量 (分配给特定路径的流量 x_k^p)
        x_vars = {}
        for flow in flows:
            # 每条流对应一个长度为可达路径数量的向量
            x_vars[flow['id']] = cp.Variable(len(flow_paths[flow['id']]), nonneg=True)

        objective_terms = []
        constraints = []

        # 3. 约束一：流量守恒 (Demand Satisfaction)
        for flow in flows:
            constraints.append(cp.sum(x_vars[flow['id']]) == flow['demand'])

        # 4. 构建链路利用率表达式与构建目标函数
        link_loads = {edge: 0 for edge in G.edges}
        
        for flow in flows:
            cls_cfg = self.classes[flow['class']]
            alpha_c = cls_cfg['alpha']
            beta_c = cls_cfg['beta']
            
            for p_idx, path in enumerate(flow_paths[flow['id']]):
                path_edges = list(zip(path[:-1], path[1:]))
                
                # 计算该路径的物理总延迟 D_p(t) 和总队列积压 \sum Q_e(t)
                path_delay = sum(G[u][v]['delay'] for u, v in path_edges)
                path_queue = sum(G[u][v]['queue'] for u, v in path_edges)
                
                # 数学公式核心：差异化 Lyapunov 权重 W_k^p(t)
                weight = self.V * alpha_c * path_delay + beta_c * path_queue
                
                # 累加至目标函数
                objective_terms.append(weight * x_vars[flow['id']][p_idx])
                
                # 累加链路负载用于后续容量约束
                for u, v in path_edges:
                    link_loads[(u, v)] += x_vars[flow['id']][p_idx]

        # 5. 约束二：链路容量硬切片约束 (Strict Capacity Constraint)
        for u, v in G.edges:
            capacity = G[u][v]['capacity']
            constraints.append(link_loads[(u, v)] <= capacity * self.rho)

        # 6. 定义问题并求解
        objective = cp.Minimize(cp.sum(objective_terms))
        prob = cp.Problem(objective, constraints)
        
        try:
            # 使用 OSQP 求解器，速度极快
            prob.solve(solver=cp.OSQP, warm_start=True)
            
            if prob.status not in ["optimal", "optimal_inaccurate"]:
                print(f"[Lyapunov] 求解失败，状态: {prob.status}")
                return None
            
            # 7. 提取结果
            results = {}
            for flow in flows:
                # 获取该流在每条路径上的分配流量，过滤掉极小值
                allocations = x_vars[flow['id']].value
                best_paths = []
                for p_idx, val in enumerate(allocations):
                    if val > 1e-3:  # 消除浮点误差
                        best_paths.append({
                            'path': flow_paths[flow['id']][p_idx],
                            'allocated_bw': float(val),
                            'weight_ratio': float(val / flow['demand'])
                        })
                results[flow['id']] = best_paths
            return results
            
        except Exception as e:
            print(f"[Lyapunov] 求解引擎崩溃: {str(e)}")
            return None

    def path_to_sid_list(self, path):
        """将节点路径转换为 SRv6 SID 列表 (倒序压栈)"""
        sid_list = []
        for node in reversed(path):
            sid_list.append(f"{self.sid_prefix}{node}::1")
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

    def run(self):
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
                    for flow_id, routes in results.items():
                        for route in routes:
                            sid_list = self.path_to_sid_list(route['path'])
                            print(f"  -> 流 [{flow_id}] | 占比 {route['weight_ratio']:.0%} | 路径: {' -> '.join(route['path'])}")
                            print(f"     SID: {sid_list}")

                            # 将策略推入 Redis 队列，让 python_bgp_gateway/sr_policy_sender.py 消费
                            policy_cmd = {
                                "flow_id": flow_id,
                                "src": route['path'][0],
                                "dst": route['path'][-1],
                                "weight": route['weight_ratio'],
                                "sids": sid_list
                            }
                            self.r.rpush(self.queue_name, json.dumps(policy_cmd))

            except KeyboardInterrupt:
                print("\n收到停止信号，Lyapunov 引擎退出...")
                break
            except Exception as e:
                print(f"[错误] 主循环异常: {e}")

if __name__ == '__main__':
    # 假设你的配置文件相对路径如下
    solver = LyapunovSolver("../config/controller.yaml")
    solver.run()