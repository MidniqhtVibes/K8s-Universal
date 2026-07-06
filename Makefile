.PHONY: builder-up builder-down builder-logs builder-test render init plan infra ping k8s check destroy clean

CONFIG ?= cluster.yaml
RUNTIME ?= .runtime
PYTHON ?= python3

builder-up:
	docker compose up --build -d

builder-down:
	docker compose down

builder-logs:
	docker compose logs -f web worker

builder-test:
	docker compose run --rm --no-deps web pytest -q

render:
	$(PYTHON) -m app.cli render --config $(CONFIG) --output $(RUNTIME) --source .

init: render
	cd $(RUNTIME)/terraform && terraform init -input=false

plan: render
	cd $(RUNTIME)/terraform && terraform plan -input=false -out=tfplan

infra:
	cd $(RUNTIME)/terraform && terraform apply -input=false tfplan

ping:
	cd $(RUNTIME)/ansible && ansible -i inventory.generated.yml all -m ping

k8s:
	cd $(RUNTIME)/ansible && ansible-playbook -i inventory.generated.yml site.yml

check:
	kubectl --kubeconfig $(RUNTIME)/kubeconfig get nodes -o wide
	kubectl --kubeconfig $(RUNTIME)/kubeconfig get pods -A
	kubectl --kubeconfig $(RUNTIME)/kubeconfig get --raw='/readyz?verbose'

destroy:
	cd $(RUNTIME)/terraform && terraform plan -destroy -input=false -out=destroy.tfplan
	@echo "Destroy-Plan erzeugt. Explizit mit 'terraform apply $(RUNTIME)/terraform/destroy.tfplan' bestätigen."

clean:
	rm -rf $(RUNTIME)
