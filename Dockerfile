FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
	# App dependencies
	gosu inotify-tools \
	&& apt-get clean \
	&& rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Install python packages
COPY pyproject.toml uv.lock ./
ENV UV_PROJECT_ENVIRONMENT=/usr/local
RUN --mount=from=ghcr.io/astral-sh/uv,source=/uv,target=/bin/uv \
	--mount=type=cache,target=/root/.cache/uv \
	uv sync --locked --compile-bytecode --no-build --no-install-project --link-mode copy

COPY src ./src

# Streamlit configuration
RUN mkdir -p .streamlit
COPY streamlit.toml .streamlit/config.toml

# Run the application
CMD [ "/app/src/entrypoint.sh" ]
