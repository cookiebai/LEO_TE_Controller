#!/usr/bin/env python3
"""
Redis 策略监听器 - 实时监听策略下发并记录到日志

使用方法：
    python3 policy_listener.py
"""

import os
import sys
import time
import json
from datetime import datetime

try:
    import redis
except ImportError:
    print("Error: redis module not found")
    sys.exit(1)

DEFAULT_REDIS_HOST = '127.0.0.1'
DEFAULT_REDIS_PORT = 6379
POLICY_QUEUE = 'policy_queue'
LOG_FILE = '/tmp/opensn_logs/policy_output.log'

os.makedirs('/tmp/opensn_logs', exist_ok=True)

def format_time():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

def main():
    r = redis.Redis(host=DEFAULT_REDIS_HOST, port=DEFAULT_REDIS_PORT, decode_responses=True)

    print(f"📡 开始监听策略下发 (policy_queue)...")
    print(f"📁 日志文件: {LOG_FILE}")
    print(f"   按 Ctrl+C 停止\n")

    last_count = 0

    while True:
        try:
            current_count = r.llen(POLICY_QUEUE)

            if current_count > last_count:
                # 有新策略
                new_policies = current_count - last_count

                # 读取所有策略
                all_policies = r.lrange(POLICY_QUEUE, 0, -1)

                with open(LOG_FILE, 'a') as f:
                    f.write(f"\n{'='*80}\n")
                    f.write(f"🎯 策略更新 @ {format_time()}\n")
                    f.write(f"{'='*80}\n")
                    f.write(f"队列长度: {current_count} 条\n\n")

                    for i, policy_str in enumerate(all_policies):
                        try:
                            policy = json.loads(policy_str)
                            f.write(f"[{i+1}] 流: {policy.get('flow_id', 'N/A')}\n")
                            f.write(f"    路径: {policy.get('src', '?')} -> {policy.get('dst', '?')}\n")
                            f.write(f"    权重: {policy.get('weight', 0):.1%}\n")
                            f.write(f"    SID: {' -> '.join(policy.get('sids', []))}\n\n")
                        except:
                            f.write(f"[{i+1}] {policy_str}\n\n")

                # 打印到控制台
                print(f"\n🎯 @ {format_time()}")
                print(f"   队列: {current_count} 条")
                for policy_str in all_policies[-3:]:  # 只显示最后3条
                    try:
                        policy = json.loads(policy_str)
                        print(f"   → {policy.get('flow_id')}: {policy.get('src')} -> {policy.get('dst')} ({policy.get('weight', 0):.0%})")
                    except:
                        pass

                last_count = current_count

            time.sleep(0.5)

        except KeyboardInterrupt:
            print("\n\n👋 监听停止")
            break
        except Exception as e:
            print(f"错误: {e}")
            time.sleep(1)

if __name__ == '__main__':
    main()
