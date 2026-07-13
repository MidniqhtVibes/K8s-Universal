# syntax=docker/dockerfile:1.7

ARG PYTHON_VERSION=3.12
ARG TERRAFORM_VERSION=1.12.2
ARG KUBECTL_VERSION=v1.36.2
ARG HELM_VERSION=v3.18.3

FROM debian:bookworm-slim AS external-tools
ARG TERRAFORM_VERSION
ARG KUBECTL_VERSION
ARG HELM_VERSION

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tar unzip \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSLo /tmp/terraform.zip "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
    && unzip /tmp/terraform.zip -d /usr/local/bin \
    && rm /tmp/terraform.zip

RUN curl -fsSLo /usr/local/bin/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod 0755 /usr/local/bin/kubectl

RUN curl -fsSLo /tmp/helm.tar.gz "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
    && tar -xzf /tmp/helm.tar.gz -C /tmp \
    && install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm \
    && rm -rf /tmp/helm.tar.gz /tmp/linux-amd64

FROM debian:bookworm-slim AS frontend-assets

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /vendor
RUN curl -fsSLo xterm.js https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js \
    && curl -fsSLo xterm.css https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css \
    && curl -fsSLo xterm-addon-fit.js https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js \
    && curl -fsSLo codemirror.js https://cdn.jsdelivr.net/npm/codemirror@5.65.16/lib/codemirror.js \
    && curl -fsSLo codemirror.css https://cdn.jsdelivr.net/npm/codemirror@5.65.16/lib/codemirror.css \
    && curl -fsSLo codemirror-yaml.js https://cdn.jsdelivr.net/npm/codemirror@5.65.16/mode/yaml/yaml.js

FROM python:${PYTHON_VERSION}-slim-bookworm AS deps-base
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN python -m venv "$VIRTUAL_ENV" \
    && pip install --no-cache-dir --upgrade pip

WORKDIR /deps

FROM deps-base AS web-deps
COPY requirements-web.txt ./
RUN pip install --no-cache-dir --no-compile -r requirements-web.txt

FROM deps-base AS worker-deps
COPY requirements.txt ./
RUN pip install --no-cache-dir --no-compile -r requirements.txt

FROM python:${PYTHON_VERSION}-slim-bookworm AS app-base
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
COPY app app
COPY --from=frontend-assets /vendor/ app/static/vendor/
COPY ansible ansible
COPY terraform terraform
COPY alembic.ini ./
COPY migrations migrations

FROM app-base AS web-base
COPY --from=web-deps /opt/venv /opt/venv
COPY --from=external-tools /usr/local/bin/kubectl /usr/local/bin/kubectl

FROM web-base AS web
RUN python -m pip uninstall -y pip \
    && find "$VIRTUAL_ENV" -type d -name "__pycache__" -prune -exec rm -rf {} + \
    && find "$VIRTUAL_ENV" -type f -name "*.pyc" -delete

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]

FROM app-base AS worker-base
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=worker-deps /opt/venv /opt/venv
COPY --from=external-tools /usr/local/bin/terraform /usr/local/bin/terraform
COPY --from=external-tools /usr/local/bin/kubectl /usr/local/bin/kubectl
COPY --from=external-tools /usr/local/bin/helm /usr/local/bin/helm
RUN ansible-playbook --syntax-check -i ansible/inventory.ini ansible/site.yml

FROM worker-base AS test
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir --no-compile -r requirements-dev.txt
COPY tests tests
COPY README.md BUILDER.md Dockerfile ./
COPY .gitattributes ./
COPY proxmox proxmox

CMD ["pytest", "-q"]

FROM worker-base AS worker
RUN python -m pip uninstall -y pip \
    && find "$VIRTUAL_ENV" -type d -name "__pycache__" -prune -exec rm -rf {} + \
    && find "$VIRTUAL_ENV" -type f -name "*.pyc" -delete

CMD ["python", "-m", "app.worker"]
