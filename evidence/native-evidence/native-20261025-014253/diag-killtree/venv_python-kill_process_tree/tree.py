import pathlib, subprocess, sys, time
c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
pathlib.Path(sys.argv[1]).write_text(str(c.pid))
time.sleep(120)
