#!/bin/bash
set -euo pipefail
cd /opt/osworld-gitlab
umask 077

if ! command -v docker-compose >/dev/null; then
  DEBIAN_FRONTEND=noninteractive apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends docker.io docker-compose curl
fi
systemctl enable --now docker
if ! swapon --noheadings --show=NAME | grep -Fxq '/osworld-gitlab.swap'; then
  if [ ! -e /osworld-gitlab.swap ]; then
    install -m 0600 /dev/null /osworld-gitlab.swap
    fallocate -l 1536M /osworld-gitlab.swap
    mkswap /osworld-gitlab.swap
  fi
  swapon /osworld-gitlab.swap
  if ! grep -Fq '/osworld-gitlab.swap' /etc/fstab; then
    echo '/osworld-gitlab.swap none swap sw 0 0' >> /etc/fstab
  fi
fi

docker-compose up -d gitlab
for attempt in $(seq 1 120); do
  if curl -fsS --noproxy '*' --max-time 5 --cacert tls/ca.crt \
       https://WINDOWS_HOST:18182/-/readiness >/dev/null 2>&1; then
    break
  fi
  if [ "$attempt" = 120 ]; then
    echo 'GitLab readiness did not pass within 20 minutes' >&2
    exit 1
  fi
  sleep 10
done

if [ ! -e gitlab-admin.token ]; then
  secret="$(openssl rand -hex 10)"
  printf '%s' "$secret" | docker-compose exec -T gitlab gitlab-rails runner "
    user = User.find_by!(username: 'root')
    token = user.personal_access_tokens.create!(
      name: 'osworld-local-benchmark', scopes: [:api, :create_runner], expires_at: 30.days.from_now)
    token.set_token(STDIN.read)
    token.save!
  " >/dev/null
  printf '%s\n' "$secret" > gitlab-admin.token.new
  unset secret
  test -s gitlab-admin.token.new
  mv gitlab-admin.token.new gitlab-admin.token
fi

python3 register_runner.py
docker-compose up -d runner
docker-compose exec -T runner gitlab-runner verify
echo 'GitLab is ready and the Runner has registered. Run the host API and Pages checks next.'
