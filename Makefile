.PHONY: environment-create environment-update backend-install backend-test backend-lint backend-run frontend-install frontend-run up down

environment-create:
	conda env create -f environment.yml

environment-update:
	conda env update -f environment.yml --prune

backend-install:
	conda run -n loop-engineering python -m pip install -e 'backend[dev]'

backend-test:
	cd backend && LOOP_ENV=test conda run --no-capture-output -n loop-engineering pytest

backend-lint:
	cd backend && conda run --no-capture-output -n loop-engineering ruff check .
	cd backend && conda run --no-capture-output -n loop-engineering ruff format --check .

backend-run:
	cd backend && LOOP_ENV=development conda run --no-capture-output -n loop-engineering uvicorn main:app --reload

frontend-install:
	pnpm install --frozen-lockfile

frontend-run:
	pnpm --dir frontend run dev

up:
	docker compose up --build

down:
	docker compose down
