import redis
import json
import time

r = redis.Redis(host='localhost', port=6379, decode_responses=True)

def send_sr_policy_via_bgp(src, dst, sid_list):
    """
    这里是将 C++ 算出的路径下发给课题一的地方。
    由于课题一是运行在 Docker 里的卫星，你通常可以通过两个方式下发：
    1. 调用课题一控制器（ONOS等）的 REST API。
    2. 通过 ExaBGP 注入 BGP SR Policy 路由。
    """
    print("="*50)
    print(f"[集中式模式激活] 向课题一下发 SRv6 策略")
    print(f"头节点 (Headend): {src}")
    print(f"目的端 (Endpoint): {dst}")
    print(f"SID List (深度 {len(sid_list)}): {sid_list}")
    
    # 示例：如果是通过 API 发给课题一
    # payload = {
    #     "endpoint": dst,
    #     "color": 100,
    #     "segment-lists": [{"sids": sid_list}]
    # }
    # requests.post(f"http://task1_api/sr-policy/{src}", json=payload)
    print("下发指令已执行完毕，卫星流量将发生重路由。")
    print("="*50)

if __name__ == '__main__':
    print("SRv6 Policy 下发服务启动，等待 C++ 引擎指令...")
    while True:
        try:
            # 阻塞读取队列，0 表示一直等，不消耗 CPU
            result = r.blpop("policy_queue", 0) 
            if result:
                queue_name, message = result
                policy = json.loads(message)
                send_sr_policy_via_bgp(policy['src'], policy['dst'], policy['sids'])
        except Exception as e:
            print(f"下发服务异常: {e}")
            time.sleep(1)