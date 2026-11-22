.PHONY: install backend frontend test

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm ci

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/python test_cross_module.py
	cd backend && .venv/bin/python test_archive_replay.py
	cd backend && .venv/bin/python test_archive_routes.py
	cd frontend && npm run typecheck
