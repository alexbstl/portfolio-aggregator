#!/usr/bin/env bash
set -e

echo "Building and starting portfolio-aggregator..."
docker compose up -d --build --force-recreate

echo "Waiting for health check..."
sleep 5

if curl -fsS http://localhost:8000/health > /dev/null 2>&1; then
    echo "Running at http://localhost:8000"
else
    echo "Container started but health check failed. Check logs:"
    echo "  docker compose logs -f"
fi
