lint:
	ruff src tests
test:
	pytest -q
run:
	docker compose exec app python -m src.realtime.ws_listener
