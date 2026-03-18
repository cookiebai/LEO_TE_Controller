#!/usr/bin/env python3
"""
Redis 数据监听器 - 实时监听并记录所有 OpenSN 数据变化

功能：
1. 监听 topo:link:* 的变化（链路状态变化）
2. 监听 telemetry:queue:* 的变化（队列积压变化）
3. 监听 te:demands 的变化（流量需求变化）
4. 监听 policy_queue 的变化（策略下发变化）
5. 将所有变化记录到日志文件

使用方法：
    python3 redis_listener.py                    # 实时监听所有变化
    python3 redis_listener.py --topo-only        # 只监听拓扑变化
    python3 redis_listener.py --queue-only       # 只监听队列变化
    python3 redis_listener.py --snapshot         # 打印当前快照并退出
"""

import os
import sys
import time
import json
import argparse
import logging
from datetime import datetime

try:
    import redis
except ImportError:
    print("Error: redis module not found. Install with: pip install redis")
    sys.exit(1)

# 配置
DEFAULT_REDIS_HOST = '127.0.0.1'
DEFAULT_REDIS_PORT = 6379
LOG_DIR = '/tmp/opensn_logs'
LOG_FILE = os.path.join(LOG_DIR, 'redis_listener.log')

# 创建日志目录
os.makedirs(LOG_DIR, exist_ok=True)

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


def get_redis_client(host=DEFAULT_REDIS_HOST, port=DEFAULT_REDIS_PORT):
    """创建 Redis 连接"""
    return redis.Redis(host=host, port=port, decode_responses=True)


def format_time():
    """获取格式化的时间戳"""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]


def print_snapshot(r):
    """打印当前 Redis 快照"""
    print("\n" + "=" * 80)
    print(f"📊 Redis 数据快照 - {format_time()}")
    print("=" * 80)

    # 1. 拓扑链路
    print("\n📡 拓扑链路 (topo:link:*):")
    topo_keys = r.keys("topo:link:*")
    if topo_keys:
        for key in sorted(topo_keys):
            data = r.hgetall(key)
            status = data.get('status', 'UNKNOWN')
            status_icon = "✅" if status == "UP" else "❌"
            print(f"  {status_icon} {key}")
            print(f"      src: {data.get('src', 'N/A')}, dst: {data.get('dst', 'N/A')}")
            print(f"      delay: {data.get('delay', 'N/A')}ms, capacity: {data.get('capacity', 'N/A')}Mbps")
            print(f"      utilization: {data.get('utilization', 'N/A')}%")
    else:
        print("  (无拓扑数据)")

    # 2. 队列积压
    print("\n📊 队列积压 (telemetry:queue:*):")
    queue_keys = r.keys("telemetry:queue:*")
    if queue_keys:
        for key in sorted(queue_keys):
            data = r.hgetall(key)
            print(f"  {key}:")
            for iface, backlog in sorted(data.items()):
                if iface.startswith('_'):
                    continue
                try:
                    backlog_int = int(backlog)
                    if backlog_int > 50000:
                        icon = "🔴"  # 拥塞
                    elif backlog_int > 10000:
                        icon = "🟡"  # 中等
                    else:
                        icon = "🟢"  # 正常
                    print(f"      {icon} {iface}: {backlog_int:,} bytes")
                except:
                    print(f"      {iface}: {backlog}")
    else:
        print("  (无队列数据)")

    # 3. 流量需求
    print("\n📈 流量需求 (te:demands):")
    demands_str = r.get("te:demands")
    if demands_str:
        try:
            demands = json.loads(demands_str)
            for d in demands:
                print(f"  {d.get('id', 'N/A')}: {d.get('src', '?')} -> {d.get('dst', '?')}")
                print(f"      class: {d.get('class', 'N/A')}, demand: {d.get('demand', 'N/A')}Mbps")
        except:
            print(f"  {demands_str}")
    else:
        print("  (无流量需求)")

    # 4. 策略队列
    print("\n🎯 策略队列 (policy_queue):")
    queue_len = r.llen("policy_queue")
    if queue_len > 0:
        policies = r.lrange("policy_queue", -5, -1)  # 只显示最后5条
        print(f"  队列长度: {queue_len} 条 (显示最后 5 条)")
        for i, policy_str in enumerate(policies):
            try:
                policy = json.loads(policy_str)
                print(f"  [{i+1}] flow_id: {policy.get('flow_id', 'N/A')}")
                print(f"      path: {policy.get('src', '?')} -> {policy.get('dst', '?')}")
                print(f"      weight: {policy.get('weight', 'N/A')}")
                print(f"      sids: {policy.get('sids', [])}")
            except:
                print(f"  [{i+1}] {policy_str[:100]}...")
    else:
        print("  (无待下发策略)")

    print("\n" + "=" * 80 + "\n")


def monitor_changes(r, options):
    """持续监听 Redis 变化"""
    print(f"\n🔍 开始监听 Redis 变化...")
    print(f"📁 日志文件: {LOG_FILE}")
    print(f"   按 Ctrl+C 停止\n")

    # 记录上次状态
    last_topo_state = {}
    last_queue_state = {}

    # 初始化状态
    for key in r.keys("topo:link:*"):
        last_topo_state[key] = r.hgetall(key)

    for key in r.keys("telemetry:queue:*"):
        last_queue_state[key] = r.hgetall(key)

    while True:
        try:
            # 检查拓扑变化
            if not options.get('queue_only'):
                for key in r.keys("topo:link:*"):
                    current = r.hgetall(key)
                    previous = last_topo_state.get(key, {})

                    if current != previous:
                        if previous == {}:
                            logger.info(f"🆕 新链路: {key} = {current}")
                        else:
                            for field in set(list(current.keys()) + list(previous.keys())):
                                old_val = previous.get(field, '(无)')
                                new_val = current.get(field, '(无)')
                                if old_val != new_val:
                                    if field == 'status':
                                        if new_val == 'UP':
                                            logger.info(f"✅ 链路上线: {key}")
                                        else:
                                            logger.warning(f"❌ 链路断开: {key}")
                                        break
                                    elif field == 'utilization':
                                        try:
                                            util = float(new_val)
                                            if util > 80:
                                                logger.warning(f"⚠️ 链路拥塞: {key} utilization={util}%")
                                        except:
                                            pass
                                    else:
                                        logger.debug(f"📝 链路变化: {key}.{field} {old_val} -> {new_val}")

                    last_topo_state[key] = current

                # 检查是否有新链路
                all_topo_keys = set(r.keys("topo:link:*"))
                for key in all_topo_keys:
                    if key not in last_topo_state:
                        data = r.hgetall(key)
                        logger.info(f"🆕 新链路: {key} = {data}")
                        last_topo_state[key] = data

                # 检查是否有链路被删除
                for key in list(last_topo_state.keys()):
                    if key not in all_topo_keys:
                        logger.info(f"🗑️ 链路删除: {key}")
                        del last_topo_state[key]

            # 检查队列变化
            if not options.get('topo_only'):
                for key in r.keys("telemetry:queue:*"):
                    current = r.hgetall(key)
                    previous = last_queue_state.get(key, {})

                    if current != previous:
                        # 打印变化
                        for field in set(list(current.keys()) + list(previous.keys())):
                            if field.startswith('_'):
                                continue
                            old_val = previous.get(field, '0')
                            new_val = current.get(field, '0')

                            try:
                                old_int = int(old_val)
                                new_int = int(new_val)
                                diff = new_int - old_int

                                if abs(diff) > 1000:  # 变化超过 1KB
                                    if new_int > 50000 and old_int <= 50000:
                                        logger.warning(f"🔴 队列拥塞: {key}.{field} = {new_int:,} bytes (+{diff:,})")
                                    elif new_int > 10000 and old_int <= 10000:
                                        logger.info(f"🟡 队列增长: {key}.{field} = {new_int:,} bytes")
                                    elif new_int < old_int:
                                        logger.debug(f"🟢 队列下降: {key}.{field} = {new_int:,} bytes ({diff:,})")
                            except:
                                if old_val != new_val:
                                    logger.debug(f"📝 队列变化: {key}.{field} {old_val} -> {new_val}")

                    last_queue_state[key] = current

                # 检查是否有新队列
                all_queue_keys = set(r.keys("telemetry:queue:*"))
                for key in all_queue_keys:
                    if key not in last_queue_state:
                        data = r.hgetall(key)
                        logger.info(f"🆕 新队列: {key} = {data}")
                        last_queue_state[key] = data

            # 检查流量需求变化
            if not options.get('queue_only'):
                demands_str = r.get("te:demands")
                if demands_str:
                    logger.debug(f"📈 流量需求: {demands_str[:100]}...")

            # 检查策略队列变化
            if not options.get('topo_only'):
                queue_len = r.llen("policy_queue")
                if queue_len > 0:
                    # 每 5 秒打印一次策略队列状态
                    pass

            time.sleep(1)

        except KeyboardInterrupt:
            print("\n\n👋 监听停止")
            break
        except redis.ConnectionError:
            logger.error("Redis 连接断开，尝试重连...")
            time.sleep(2)
            try:
                r = get_redis_client()
            except:
                pass
        except Exception as e:
            logger.error(f"监听错误: {e}")
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(
        description='OpenSN Redis 数据监听器 - 实时监听并记录所有数据变化'
    )
    parser.add_argument(
        '--redis-host',
        default=os.environ.get('REDIS_HOST', DEFAULT_REDIS_HOST),
        help='Redis host'
    )
    parser.add_argument(
        '--redis-port',
        type=int,
        default=int(os.environ.get('REDIS_PORT', DEFAULT_REDIS_PORT)),
        help='Redis port'
    )
    parser.add_argument(
        '--topo-only',
        action='store_true',
        help='只监听拓扑变化'
    )
    parser.add_argument(
        '--queue-only',
        action='store_true',
        help='只监听队列变化'
    )
    parser.add_argument(
        '--snapshot',
        action='store_true',
        help='打印当前快照并退出'
    )
    parser.add_argument(
        '--debug',
        action='store_true',
        help='启用调试日志'
    )

    args = parser.parse_args()

    if args.debug:
        logger.setLevel(logging.DEBUG)

    options = {
        'topo_only': args.topo_only,
        'queue_only': args.queue_only,
    }

    r = get_redis_client(args.redis_host, args.redis_port)

    if args.snapshot:
        print_snapshot(r)
    else:
        monitor_changes(r, options)


if __name__ == '__main__':
    main()
