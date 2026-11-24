"""Windows QEMU transport for the pinned official V2 guest, separate from V1."""
import subprocess
import os
import time
import uuid
from pathlib import Path
import requests

class NativeQemuProvider:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.output.mkdir(parents=True, exist_ok=True)
        self.process = None
        root = Path('H:/GUI_AGENT_OSWorld/qemu_portable')
        self.qemu = next(root.rglob('qemu-system-x86_64.exe'))
        self.img = next(root.rglob('qemu-img.exe'))
        self.base = next(Path('H:/GUI_AGENT_OSWorld/v2_official_image').rglob('*.qcow2'))
        self.disk_dir = Path('H:/GUI_AGENT_OSWorld/v2_work_disks') / uuid.uuid4().hex
        self.overlay = self.disk_dir / 'working.qcow2'
        self.overlay.parent.mkdir(parents=True, exist_ok=True)

    def start_emulator(self, path_to_vm, headless, os_type) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        if not self.overlay.exists():
            subprocess.run([str(self.img), 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', str(self.base), str(self.overlay)], check=True, capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        net = 'user,id=net0,hostfwd=tcp:127.0.0.1:5002-:5000,hostfwd=tcp:127.0.0.1:9223-:9222,hostfwd=tcp:127.0.0.1:8082-:8080,hostfwd=tcp:127.0.0.1:3002-:3000,hostfwd=tcp:127.0.0.1:8002-:8000'
        memory_mb = int(os.environ.get('OSWORLD_QEMU_MEMORY_MB', '4096'))
        if memory_mb < 2048:
            raise ValueError('Diagnostic guest requires at least 2048 MB')
        common = [str(self.qemu), '-m', str(memory_mb), '-smp', '4', '-drive', f'file={self.overlay.as_posix()},format=qcow2,if=virtio', '-netdev', net, '-device', 'virtio-net-pci,netdev=net0', '-vga', 'std', '-display', 'none', '-vnc', '127.0.0.1:2', '-no-reboot']
        for accelerator in ['whpx', 'tcg']:
            self.log = (self.output / f'qemu_{accelerator}.log').open('w', encoding='utf-8')
            self.process = subprocess.Popen(common + ['-accel', accelerator], cwd=self.qemu.parent, stdout=self.log, stderr=self.log, creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(5)
            if self.process.poll() is None:
                self.accelerator = accelerator
                return
            self.log.close()
        raise RuntimeError('QEMU failed with WHPX and TCG; see isolated guest logs')

    def get_ip_address(self, path_to_vm) -> str:
        session = requests.Session()
        session.trust_env = False
        for _ in range(180):
            if self.process.poll() is not None:
                raise RuntimeError('Official V2 guest process exited before ready')
            try:
                response = session.get('http://127.0.0.1:5002/screenshot', timeout=3)
                if response.ok and (response.headers.get('Content-Type', '').startswith('image/') or response.content.startswith(b'\x89PNG')):
                    return '127.0.0.1:5002:9223:5902:8082'
            except requests.RequestException:
                pass
            time.sleep(5)
        raise TimeoutError('Official V2 guest control server unavailable after 15 minutes')

    def stop_emulator(self, path_to_vm, *args, **kwargs) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if hasattr(self, 'log'):
            self.log.close()

    def revert_to_snapshot(self, path_to_vm, snapshot_name):
        self.stop_emulator(path_to_vm)
        # Preserve interrupted disks; use a fresh overlay rather than deleting.
        self.overlay = self.disk_dir / f'working_{time.time_ns()}.qcow2'
        return path_to_vm
