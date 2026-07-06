FROM python:3.12-slim-bookworm

ARG TERRAFORM_VERSION=1.12.2
ARG KUBECTL_VERSION=v1.33.2
ARG HELM_VERSION=v3.18.3

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl openssh-client unzip \
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

WORKDIR /workspace
COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY app app
COPY tests tests
COPY ansible ansible
RUN curl -fsSLo app/static/htmx.min.js https://unpkg.com/htmx.org@2.0.6/dist/htmx.min.js
RUN mkdir -p app/static/vendor \
    && curl -fsSLo app/static/vendor/xterm.js https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js \
    && curl -fsSLo app/static/vendor/xterm.css https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css \
    && curl -fsSLo app/static/vendor/xterm-addon-fit.js https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js
RUN ansible-playbook --syntax-check -i ansible/inventory.ini ansible/site.yml
COPY alembic.ini .
COPY migrations migrations

EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
