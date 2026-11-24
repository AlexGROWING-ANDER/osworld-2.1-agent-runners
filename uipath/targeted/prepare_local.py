"""Stage a dedicated OSWorld GitLab VM. Run with --start only after freeing memory."""
import argparse
import datetime as dt
import hashlib
import io
import ipaddress
import json
from pathlib import Path
import subprocess
import sys

import certifi
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


ROOT = Path(__file__).resolve().parent
QEMU = ROOT.parents[1] / "qemu_portable"
BASE = ROOT / "debian-12-genericcloud-amd64.qcow2"
PRIVATE = ROOT / "local_keys"
LOCATION = ROOT / "vm-disk-location.txt"
OVERLAY = Path(LOCATION.read_text(encoding="utf-8-sig").strip()) if LOCATION.is_file() else PRIVATE / "gitlab-debian.qcow2"
SEED = PRIVATE / "gitlab-seed.iso"
IMAGE_MEMORY_MIB = 2560
GUEST_DISK_GIB = 28


def write(path, content):
    path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)


def tls_files(host_ip, pages_ip):
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)
    end = start + dt.timedelta(days=60)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "OSWorld local GitLab CA")])
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, str(host_ip))])

    def certificate(name, key):
        return (x509.CertificateBuilder().subject_name(name).issuer_name(issuer)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(start).not_valid_after(end))

    ca_cert = certificate(issuer, ca_key).add_extension(
        x509.BasicConstraints(ca=True, path_length=0), critical=True,
    ).sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM)
    server_cert = (certificate(subject, server_key)
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(host_ip), x509.IPAddress(pages_ip)]), critical=False)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .sign(ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
    server_pem = server_key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
    )
    return ca_cert, server_cert, server_pem


def stage_seed(host_ip, pages_ip):
    if SEED.exists():
        saved_host = (ROOT / "host-only-ip.txt").read_text().strip()
        if saved_host != f"{host_ip} {pages_ip}":
            raise ValueError("The existing seed belongs to a different host-only address")
        return
    wheels = list((ROOT / "python_packages").glob("pycdlib-*.whl"))
    if len(wheels) != 1:
        raise FileNotFoundError("Expected one local pycdlib wheel")
    sys.path.insert(0, str(wheels[0]))
    import pycdlib

    PRIVATE.mkdir(exist_ok=True)
    account = subprocess.check_output([
        "powershell.exe", "-NoProfile", "-Command",
        "[Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
    ], text=True).strip()
    subprocess.run(["icacls.exe", str(PRIVATE), "/inheritance:r", "/grant:r",
                    "*" + account + ":(OI)(CI)F"], check=True, capture_output=True)
    ssh_key = PRIVATE / "id_ed25519"
    if not ssh_key.exists():
        subprocess.run(["ssh-keygen.exe", "-q", "-t", "ed25519", "-N", "", "-f", str(ssh_key)], check=True)

    ca, cert, server_key = tls_files(host_ip, pages_ip)
    write(ROOT / "local-ca.crt", ca)
    write(ROOT / "requests-ca-bundle.pem", Path(certifi.where()).read_bytes() + b"\n" + ca)
    write(ROOT / "host-only-ip.txt", f"{host_ip} {pages_ip}\n")
    host = str(host_ip)
    files = []

    def include(path, content, permissions="0644"):
        files.append({"path": path, "content": content, "permissions": permissions})

    install = "/opt/osworld-gitlab"
    for name in ("compose.template.yml", "bootstrap.sh", "register_runner.py"):
        text = (ROOT / name).read_text(encoding="utf-8-sig").replace("WINDOWS_HOST", host).replace("PAGES_HOST", str(pages_ip))
        target = "docker-compose.yml" if name == "compose.template.yml" else name
        include(f"{install}/{target}", text, "0755" if name == "bootstrap.sh" else "0644")
    include(f"{install}/tls/ca.crt", ca.decode())
    include(f"{install}/tls/server.crt", cert.decode())
    include(f"{install}/tls/server.key", server_key.decode(), "0600")
    include("/usr/local/share/ca-certificates/osworld-gitlab.crt", ca.decode())
    include("/etc/systemd/system/osworld-gitlab-address.service", f"""[Unit]
Description=Route GitLab's external host-only address locally inside its own VM
Before=docker.service

[Service]
Type=oneshot
ExecStart=/usr/sbin/ip address replace {host}/32 dev lo
ExecStart=/usr/sbin/ip address replace {pages_ip}/32 dev lo
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
""")
    config = {
        "hostname": "osworld-local-gitlab",
        "ssh_pwauth": False,
        "disable_root": True,
        "users": [{
            "name": "osworld",
            "shell": "/bin/bash",
            "lock_passwd": True,
            "sudo": "ALL=(ALL) NOPASSWD:ALL",
            "ssh_authorized_keys": [ssh_key.with_suffix(".pub").read_text().strip()],
        }],
        "write_files": files,
        "runcmd": [
            ["mkdir", "-p", f"{install}/runner"],
            ["update-ca-certificates"],
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "--now", "osworld-gitlab-address.service"],
        ],
    }
    records = {
        "user-data": "#cloud-config\n" + yaml.safe_dump(config, sort_keys=False),
        "meta-data": "instance-id: osworld-gitlab-001\nlocal-hostname: osworld-local-gitlab\n",
    }
    # Restrict the generated seed, because its payload includes the VM's TLS key.
    private_seed = SEED.with_suffix(".iso.partial")
    iso = pycdlib.PyCdlib()
    iso.new(interchange_level=3, rock_ridge="1.09", vol_ident="CIDATA")
    try:
        for name, content in records.items():
            buf = content.encode()
            iso.add_fp(io.BytesIO(buf), len(buf), "/" + name.upper().replace("-", "_") + ";1", rr_name=name)
        iso.write(str(private_seed))
    finally:
        iso.close()
    private_seed.replace(SEED)


def verify_base():
    if not BASE.is_file():
        raise FileNotFoundError("The verified Debian image has not been downloaded yet")
    checksum_files = [ROOT / "SHA512SUMS", ROOT / "selected-image.SHA512SUMS"]
    expected = "\n".join(p.read_text(encoding="utf-8") for p in checksum_files if p.is_file())
    matches = [line.split()[0].lower() for line in expected.splitlines()
               if line.split() and line.split()[-1].lstrip("*") == BASE.name]
    with BASE.open("rb") as image:
        actual = hashlib.file_digest(image, "sha512").hexdigest()
    if actual not in matches:
        raise ValueError("The Debian base image checksum does not match")


def prepare_disk():
    verify_base()
    if not OVERLAY.exists():
        subprocess.run([str(QEMU / "qemu-img.exe"), "create", "-f", "qcow2", "-F", "qcow2",
                        "-b", str(BASE), str(OVERLAY), f"{GUEST_DISK_GIB}G"], check=True)


def start_guest(host_ip, pages_ip, memory_mib):
    mem_cmd = (
        "$s=Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory; "
        "[pscustomobject]@{free=$s.AvailableMBytes;commit=[int](($s.CommitLimit-$s.CommittedBytes)/1MB)}|ConvertTo-Json -Compress"
    )
    mem = json.loads(subprocess.check_output(
        ["powershell.exe", "-NoProfile", "-Command", mem_cmd], text=True,
    ))
    if mem["free"] < 900 or mem["commit"] < memory_mib + 2600:
        raise RuntimeError(f"Host memory insufficient for GitLab VM: {mem}")
    ports = ["hostfwd=tcp:127.0.0.1:18722-:22"] + [
        f"hostfwd=tcp:{host_ip}:{port}-:{port}" for port in (18182, 18723)
    ]
    ports.append(f"hostfwd=tcp:{pages_ip}:18183-:18183")
    command = [
        str(QEMU / "qemu-system-x86_64.exe"), "-machine", "q35,accel=whpx",
        "-m", str(memory_mib), "-smp", "2", "-display", "none",
        "-qmp", "tcp:127.0.0.1:18724,server=on,wait=off",
        "-drive", f"file={OVERLAY.as_posix()},format=qcow2,if=virtio,discard=unmap,detect-zeroes=unmap",
        "-drive", f"file={SEED.as_posix()},format=raw,if=virtio,readonly=on",
        "-netdev", "user,id=nic," + ",".join(ports), "-device", "virtio-net-pci,netdev=nic",
        "-serial", f"file:{(ROOT / 'vm-serial.log').as_posix()}",
    ]
    with (ROOT / "qemu.log").open("ab") as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    print(f"GitLab VM process started, PID {proc.pid}. Check cloud-init and GitLab separately.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-only-ip", type=ipaddress.IPv4Address, required=True)
    parser.add_argument("--pages-ip", type=ipaddress.IPv4Address, required=True)
    parser.add_argument("--memory-mib", type=int, default=IMAGE_MEMORY_MIB)
    parser.add_argument("--start", action="store_true")
    args = parser.parse_args()
    if args.memory_mib < 2048:
        parser.error("GitLab needs at least 2048 MiB of guest RAM")
    if args.host_only_ip == args.pages_ip:
        parser.error("Use a separate host-only address for Pages")
    stage_seed(args.host_only_ip, args.pages_ip)
    if BASE.exists() or args.start:
        prepare_disk()
    if args.start:
        start_guest(args.host_only_ip, args.pages_ip, args.memory_mib)
    else:
        print("GitLab VM seed and Compose staged. No virtual machine was started.")


if __name__ == "__main__":
    main()
