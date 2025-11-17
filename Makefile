SHELL := /bin/bash

.PHONY: build up down

build:
	set -a && . .env && docker build -t samples:$$VERSION .

up: down
	docker compose up

down:
	docker compose down
