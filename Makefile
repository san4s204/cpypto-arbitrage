install:
	python -m pip install -r requirements.txt

install-dev:
	python -m pip install -r requirements-dev.txt

install-legacy:
	python -m pip install -r requirements-legacy.txt

lint:
	python -m ruff check app tests

test:
	python -m pytest -q tests

run:
	python -m app.main

select-universe:
	python -m app.research.select_universe

compose-up:
	docker compose up --build

compose-down:
	docker compose down
