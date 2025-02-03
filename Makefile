# Makefile for building and tagging the Docker container

# Variables (adjust as needed)
IMAGE_NAME := telemedia-bot
TAG := latest
DOCKERFILE := Dockerfile

TELEMEDIA_DB := ./data/db
TELEMEDIA_CONFIG := ./data/config
TELEMEDIA_MOVIES := ./data/movies
TELEMEDIA_TV := ./data/tv
TELEMEDIA_DOWNLOADS := ./data/downloads
SMA_CONFIG := ./data/config/sma_config


# Include .env file if it exists
-include .env

# Export all variables to sub-processes
export

# Ensure TELEGRAM_BOT_TOKEN, PUID, and PGID are set before allowing run.
ifndef TELEGRAM_BOT_TOKEN
$(error TELEGRAM_BOT_TOKEN environment variable is not set. Please export TELEGRAM_BOT_TOKEN before running 'make run'.)
endif

.PHONY: build run push clean

# Build the Docker image and tag it as "latest"
build:
	docker build -t $(IMAGE_NAME):$(TAG) -f $(DOCKERFILE) .

# Run the container with user defined by PUID and PGID, volume mounts, and exported environment variables.
run:
	docker run -d \
	  -v $(TELEMEDIA_DB):/app/db \
	  -v $(TELEMEDIA_CONFIG):/app/config \
	  -v $(TELEMEDIA_MOVIES):/app/movies \
	  -v $(TELEMEDIA_TV):/app/tv \
	  -v $(TELEMEDIA_DOWNLOADS):/app/downloads \
	  -v $(SMA_CONFIG):/app/sickbeard_mp4_automator/config \
	  -e TELEGRAM_BOT_TOKEN="$(TELEGRAM_BOT_TOKEN)" \
	  -e TELETHON_API_ID="$(TELETHON_API_ID)" \
	  -e TELETHON_API_HASH="$(TELETHON_API_HASH)" \
	  --name $(IMAGE_NAME) \
	  $(IMAGE_NAME):$(TAG)

# Push the image to a Docker registry
push:
	docker push $(IMAGE_NAME):$(TAG)

# Remove the container (if needed)
clean:
	-docker rm -f $(IMAGE_NAME)