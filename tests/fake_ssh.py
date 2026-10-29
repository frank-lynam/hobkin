"""A stand-in for ssh in the tests: runs the remote command here, as if on another machine whose home is
$FAKE_SSH_HOME and which sees none of our environment. Host "down" can't be reached. ssh's options are ignored."""
import os, subprocess, sys
args, i = sys.argv[1:], 0
while i < len(args) and args[i].startswith("-"):
    i += 2 if args[i] in ("-o", "-p", "-i", "-l", "-F", "-J") else 1
host, command = args[i], " ".join(args[i + 1:])
if host == "down":
    sys.stderr.write("ssh: connect to host down port 22: Connection refused\n")
    sys.exit(255)
home = os.environ["FAKE_SSH_HOME"]
os.makedirs(home, exist_ok=True)
env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "USER": "remote", "LANG": "C.UTF-8"}
sys.exit(subprocess.run(["bash", "-c", command], env=env, cwd=home).returncode)
