FROM python:3.12-slim

ARG UV_VERSION=0.12.7
ARG PYTEST_VERSION=8.3.5
ARG RUFF_VERSION=0.9.10
ARG MYPY_VERSION=1.15.0

RUN python -m pip install --no-cache-dir \
    "uv==${UV_VERSION}" \
    "pytest==${PYTEST_VERSION}" \
    "ruff==${RUFF_VERSION}" \
    "mypy==${MYPY_VERSION}"

WORKDIR /workspace
