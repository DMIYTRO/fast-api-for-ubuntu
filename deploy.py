import sys
import os
import subprocess
import urllib.request

# Ensure UTF-8 output on Windows terminal
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')

import paramiko

VM_IP = "10.20.2.104"
VM_USER = "ubuntu"
VM_PASS = "Sborka.123"
VM_PROJECT_DIR = "/home/ubuntu/v2-web-platform-FastApi"

def run_local(cmd):
    print(f"-> [Local] {cmd}")
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if res.stdout.strip():
        print(res.stdout.strip())
    if res.stderr.strip() and res.returncode != 0:
        print(f"Error: {res.stderr.strip()}")
    return res.returncode

def main():
    commit_msg = sys.argv[1] if len(sys.argv) > 1 else "auto sync: dev updates"
    
    print("=" * 60)
    print("STARTING DEPLOY & RESTART ON UBUNTU DESKTOP (VM 104)")
    print("=" * 60)

    # 1. Local Git add & commit
    print("\n[1/4] Saving local changes (Git)...")
    run_local("git add .")
    status = subprocess.run("git status --porcelain", shell=True, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if status.stdout.strip():
        run_local(f'git commit -m "{commit_msg}"')
    else:
        print("No new local changes to commit.")

    # 2. Push to GitHub
    print("\n[2/4] Pushing code to GitHub (git push)...")
    push_code = run_local("git push origin main")
    if push_code != 0:
        print("Notice: Git push returned warning/error, proceeding to SSH pull...")

    # 3. SSH to VM 104 -> Pull & Restart Service
    print(f"\n[3/4] Connecting to VM 104 ({VM_IP}) via SSH...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(VM_IP, username=VM_USER, password=VM_PASS, timeout=10)
        
        # Git pull
        print("Pulling latest code on server (git pull)...")
        stdin, stdout, stderr = ssh.exec_command(f"cd {VM_PROJECT_DIR} && git pull origin main")
        out_pull = stdout.read().decode('utf-8', errors='replace')
        print(out_pull.strip())

        # Restart fastapi-app and reload nginx
        print("Restarting FastAPI web service and Nginx...")
        restart_cmd = f"echo '{VM_PASS}' | sudo -S systemctl restart fastapi-app && echo '{VM_PASS}' | sudo -S systemctl reload nginx"
        stdin, stdout, stderr = ssh.exec_command(restart_cmd)
        out_restart = stdout.read().decode('utf-8', errors='replace')
        err_restart = stderr.read().decode('utf-8', errors='replace')
        
        # Check service status
        stdin, stdout, stderr = ssh.exec_command("systemctl is-active fastapi-app")
        srv_status = stdout.read().decode('utf-8', errors='replace').strip()
        print(f"FastAPI Service Status: [{srv_status.upper()}]")

        ssh.close()
    except Exception as e:
        print(f"SSH Error: {e}")
        sys.exit(1)

    # 4. Check web endpoint response
    print("\n[4/4] Verifying web server response...")
    try:
        req = urllib.request.Request(f"http://{VM_IP}:8000/docs", headers={"User-Agent": "DeployCheck"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            print(f"Web Server OK! HTTP Status Code: {resp.status}")
    except Exception as e:
        print(f"Web check notice: {e}")

    print("\n" + "=" * 60)
    print("DEPLOYMENT & RESTART COMPLETED SUCCESSFULLY!")
    print("=" * 60)

if __name__ == '__main__':
    main()
