#!/bin/bash
set -euo pipefail
cd /opt/osworld-gitlab
umask 077

package=gitlab-ce_18.7.0-ce.0_amd64.deb
sha=b7215bdf07ecc61cc869ef0779cdef30fa03edb89a565aa5a84da6ad71bef21e
bytes=1398428818
for i in $(seq 1 150); do
  if [ -e "$package" ] && [ "$(stat -c %s "$package")" = "$bytes" ]; then
    break
  fi
  if ! systemctl is-active --quiet osworld-gitlab-package; then
    echo 'Alternate GitLab package transfer stopped before completion' >&2
    exit 1
  fi
  sleep 15
done
printf '%s  %s\n' "$sha" "$package" | sha256sum -c -
test "$(dpkg-deb -f "$package" Version)" = 18.7.0-ce.0

for i in $(seq 1 120); do
  if command -v docker-compose >/dev/null && systemctl is-active --quiet docker; then
    break
  fi
  sleep 10
done
command -v docker-compose >/dev/null
systemctl is-active --quiet docker

if docker-compose ps -q gitlab | grep -q .; then
  echo 'The Docker-based GitLab container is already up. Keeping the initial deployment path.'
  exit 0
fi

for prior in $(pgrep -f '^/bin/bash /opt/osworld-gitlab/bootstrap.sh' || true); do
  # This isolated VM was created for this task. Stop only its initial Docker GitLab pull.
  for child in $(pgrep -P "$prior" || true); do
    command_line=$(tr '\0' ' ' < "/proc/$child/cmdline" 2>/dev/null || true)
    case "$command_line" in
      *"docker-compose up -d gitlab"*) kill -TERM "$prior" "$child" || true ;;
      *"apt-get"*) echo 'Docker package installation has not completed yet' >&2; exit 1 ;;
    esac
  done
done

if docker-compose ps -q gitlab | grep -q .; then
  echo 'The original GitLab container is already up.'
  exit 0
fi
install -d -m 0700 /etc/gitlab /etc/gitlab/trusted-certs
install -m 0644 tls/ca.crt /etc/gitlab/trusted-certs/osworld-ca.crt

if ! swapon --noheadings --show=NAME | grep -Fxq '/osworld-gitlab.swap'; then
  if [ ! -e /osworld-gitlab.swap ]; then
    install -m 0600 /dev/null /osworld-gitlab.swap
    fallocate -l 1536M /osworld-gitlab.swap
    mkswap /osworld-gitlab.swap
  fi
  swapon /osworld-gitlab.swap
  grep -Fq '/osworld-gitlab.swap' /etc/fstab ||
    echo '/osworld-gitlab.swap none swap sw 0 0' >> /etc/fstab
fi

python3 - <<'PY'
from pathlib import Path
import yaml

compose = Path('docker-compose.yml')
source = yaml.safe_load(compose.read_text())
server = source['services']['gitlab']['environment']['GITLAB_OMNIBUS_CONFIG']
Path('/etc/gitlab/gitlab.rb').write_text(server.replace('/etc/osworld-tls/', '/opt/osworld-gitlab/tls/'))
runner = source['services']['runner']
runner.pop('depends_on', None)
Path('docker-compose.native-runner.yml').write_text(yaml.safe_dump({'version': '3.8', 'services': {'runner': runner}}))
PY
touch /etc/gitlab/skip-auto-reconfigure
DEBIAN_FRONTEND=noninteractive apt-get install -y "./$package"
gitlab-ctl reconfigure
rm -f /etc/gitlab/skip-auto-reconfigure

printf '%s\n' 'Port 22' 'Port 18723' > /etc/ssh/sshd_config.d/80-osworld-gitlab.conf
sshd -t
systemctl reload ssh

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
