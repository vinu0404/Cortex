.PHONY: up down build restart logs migrate seed shell clean nuke

APP = cortex_app-app-1
WORKER = cortex_app-celery_worker-1

up:
	docker compose up -d

build:
	docker compose up -d --build

down:
	docker compose down

restart:
	docker compose down && docker compose up -d --build

migrate:
	docker exec $(APP) alembic upgrade head

seed:
	docker exec $(APP) python seed_langfuse.py

setup: build migrate seed

logs:
	docker compose logs -f app celery_worker

logs-worker:
	docker compose logs -f celery_worker

logs-all:
	docker compose logs -f

shell:
	docker exec -it $(APP) bash

ps:
	docker compose ps

clean:
	docker compose down -v

nuke:
	docker compose down -v --rmi all
