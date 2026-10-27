#!/usr/bin/env bash
# Sourced helper: remote environment for the simulated host (no systemd inside the container).
# Usage: source deploy/sim_env.sh; deploy/remote_deploy.sh ... "${SIM_ENV_ARGS[@]}"
# shellcheck shell=bash disable=SC2034
SIM_ENV_ARGS=(
  --env "SERVICE_CTL=sudo /usr/local/bin/agentctl"
  --env "NGINX_TEST_CMD=sudo /usr/sbin/nginx -t"
  --env "NGINX_RELOAD_CMD=sudo /usr/sbin/nginx -s reload"
  --env "NGINX_LOCAL_PORT=80"
)
