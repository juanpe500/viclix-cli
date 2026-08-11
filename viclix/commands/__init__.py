"""Command handlers for the Viclix CLI, one module per group.

cli.py imports the cmd_* functions from here and dispatches to them. Each module
below carries its own MIGRATION guide listing exactly what to paste from the old
monolithic cli.py.

  auth.py      login / logout / whoami / setup / disconnect / update + wizards
  project.py   init / link / delete / open (live URL) / dash (dashboard page)
  deploy.py    deploy / hotfix / first-time deploy
  run.py       run / local / config-run + venv/uvicorn/local-env helpers
  download.py  env / download + download helpers
  inspect.py   exec / pip-install / db / fs / read / rollback / env-set /
               env-unset / probe / db-restore / db-exec + the ENDPOINTS table
  agents.py    agent-run / agent-status / agents (conversations) / fleet
"""
