COGITATOR deliverables

Files:
- index.html
- bridge.py
- bridge_daemon.py
- sw.js
- manifest.webmanifest
- icon.svg
- test_e2e.py

Windows 11 quick start:
1. Put all files in one folder.
2. Run: python test_e2e.py
3. Optional syntax checks:
   python -m py_compile bridge.py
   python -m py_compile bridge_daemon.py
4. Serve the app:
   python -m http.server 8080
   then open http://localhost:8080/index.html
5. Run daemon:
   python bridge_daemon.py
