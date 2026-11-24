#!/bin/bash
set -euo pipefail
cd /opt/osworld-gitlab
umask 077
gitlab_url=$(sed -n "s/^external_url '\([^']*\)'/\1/p" /etc/gitlab/gitlab.rb)
for attempt in $(seq 1 120); do
  if curl -fsS --noproxy '*' --max-time 5 --cacert tls/ca.crt "$gitlab_url/users/sign_in" >/dev/null 2>&1; then
    break
  fi
  if [ "$attempt" = 120 ]; then
    echo 'Native GitLab readiness did not pass within 20 minutes' >&2
    exit 1
  fi
  sleep 10
done

if [ ! -e gitlab-admin.token ]; then
  secret="$(openssl rand -hex 10)"
  printf '%s' "$secret" | gitlab-rails runner "
    user = User.find_by!(username: 'root')
    token = user.personal_access_tokens.create!(
      name: 'osworld-local-benchmark', scopes: [:api, :create_runner], expires_at: 30.days.from_now)
    token.set_token(STDIN.read)
    token.save!
  " >/dev/null
  printf '%s\n' "$secret" > gitlab-admin.token.new
  unset secret
  mv gitlab-admin.token.new gitlab-admin.token
fi

export COMPOSE_FILE=/opt/osworld-gitlab/docker-compose.native-runner.yml
python3 register_runner.py
docker-compose up -d runner
docker-compose exec -T runner gitlab-runner verify
echo 'GitLab 18.7.0 (verified Linux package) and Docker CI runner are ready.'
