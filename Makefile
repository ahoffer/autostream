SHELL := /bin/bash

.PHONY: build up down

build:
	set -a && . .env && docker build -t $$CONTAINER_NAME:$$VERSION .

up:
	docker compose up

down:
	docker compose down
