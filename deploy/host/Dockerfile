# Simulated deployment host: Debian + sshd + nginx + git + python venv + agentctl.
# It behaves like the EC2 instance the blue/green scripts target, so the real scripts can be
# exercised end to end in CI or locally. Build from the repository root:
#   docker build -f deploy/host/Dockerfile -t t18ej2-host:local .
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        openssh-server nginx python3 python3-venv python3-pip git curl ca-certificates sudo \
        procps util-linux \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf \
    && mkdir -p /run/sshd /run/agent /var/log/agent /etc/agent \
    && useradd --create-home --shell /bin/bash deploy \
    && usermod -p '*' deploy \
    && mkdir -p /home/deploy/.ssh /opt/agent \
    && chmod 700 /home/deploy/.ssh \
    && chown -R deploy:deploy /home/deploy /opt/agent /var/log/agent \
    && rm -f /etc/ssh/ssh_host_* \
    && ssh-keygen -q -t ed25519 -N "" -f /etc/ssh/ssh_host_ed25519_key

# Only ed25519 host key, key-only authentication, a single allowed user.
RUN printf '%s\n' \
      'Port 22' \
      'HostKey /etc/ssh/ssh_host_ed25519_key' \
      'PasswordAuthentication no' \
      'KbdInteractiveAuthentication no' \
      'PermitRootLogin no' \
      'AllowUsers deploy' \
      'PubkeyAuthentication yes' \
      'UsePAM no' \
      'Subsystem sftp /usr/lib/openssh/sftp-server' \
      > /etc/ssh/sshd_config

# The deploy user may only control the agent services and nginx, like on a real host.
RUN printf '%s\n' \
      'deploy ALL=(root) NOPASSWD: /usr/local/bin/agentctl, /usr/sbin/nginx' \
      > /etc/sudoers.d/deploy \
    && chmod 440 /etc/sudoers.d/deploy

COPY deploy/nginx/agent.conf /etc/nginx/conf.d/agent.conf
COPY deploy/host/agentctl.sh /usr/local/bin/agentctl
COPY deploy/host/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/agentctl /usr/local/bin/entrypoint.sh \
    && chown deploy:deploy /etc/nginx/conf.d/agent.conf

# Wheels for the pinned requirements, so every release venv installs offline and fast.
COPY requirements.txt /tmp/requirements.txt
RUN python3 -m pip download --quiet --disable-pip-version-check \
        -r /tmp/requirements.txt -d /opt/agent/wheelhouse \
    && chown -R deploy:deploy /opt/agent

# Settings shared by both slots; the mock provider needs no secrets.
RUN printf '%s\n' 'LLM_PROVIDER=mock' 'APP_ENV=simulated' 'LOG_LEVEL=INFO' > /etc/agent/agent.env

EXPOSE 22 80
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
