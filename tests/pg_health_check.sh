#!/bin/bash
# Check the postgres-database pod's healthcheck command and that it no longer logs FATAL role errors.
c=postgres-database
docker inspect "$c" --format '{{json .Config.Healthcheck.Test}} {{.State.Health.Status}}'
echo "FATAL role lines in the last 90 s: $(docker logs --since 90s "$c" 2>&1 | grep -c 'FATAL:  role')"
