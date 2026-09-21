#!/usr/bin/with-contenv bashio
bashio::log.info "Starting SDS200 bridge..."
exec python3 /app/main.py
