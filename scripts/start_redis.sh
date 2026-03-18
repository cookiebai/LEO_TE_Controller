#!/bin/bash
# scripts/start_redis.sh
echo "启动 Redis 容器..."
docker run -d --name leo-redis -p 6379:6379 redis:alpine
echo "Redis 启动成功，端口 6379"